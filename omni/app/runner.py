"""Assemble and drive the running app.

`build_loop` wires the spine's collaborators into an `AppLoop` from a schedule, a
feed fetcher, and a display sink — the one place the standard wiring lives, so the
emulator entry, a future hardware entry, and tests all build the loop the same way. A plain
provider is asked inline, once a refresh, through a `ProviderSupervisor`; a scope source
hands the board its scopes itself.
`build_planned_loop` is the running MLB board's wiring on top of it: every source read from a
`PlannedMlb`, which hands the board its slate date by date, so a refresh never waits on the network,
and the live win meter off unless asked for. The plan does the retrying: nothing backs off on top of it,
and while its quota ledger holds a date's slate back, the league's health says why.
Either builder takes NFL too (`NflSource`): its slate beside MLB's, and its games carded from the answers its
feeds hold, held back for NFL's own TV delay. Without one, the board is MLB's alone; without MLB's, NFL's.
`board_plan` is the one `PollPlan` both leagues ask through, each ask spending its own provider's account, and
`planned_nfl_source` puts NFLMeta on it (`PlannedNfl`), metered against the quota ledger.
Both builders take the board policy's user settings (`omni.policy.settings.PolicySettings`) for the board.
`run_forever` is the thin real-time wrapper: refreshes on a thread of their own, and frames on
this one at a steady rate, so a slow fetch never freezes the panel. Each frame (`paint_frame`)
counts from when the display took it, so a slow draw or swap doesn't eat into a card's time. A
refresh runs whenever a poll answer lands, when the last one said something would change by itself
(a play clearing the delay, the source going stale), and at least every few seconds. Given a
`Poller`, it warms the plan up first (the slate, then its games, then a pass that can show them)
and then leaves the fetching to the poller's workers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from omni.app.background import Waking
from omni.app.contest_store import ContestStore
from omni.app.display import DisplaySink
from omni.app.frame_stream import AcknowledgingSink
from omni.app.loop import AppLoop, Painted, QuotaNote
from omni.app.nfl_feeds import NflFeeds
from omni.app.nfl_pipeline import LiveFootballPipeline
from omni.app.planned_mlb import MLB_POLICIES, PlannedMlb
from omni.app.planned_nba import MeteredBdl, PlannedNba, nba_accounts, nba_policies
from omni.app.planned_nfl import NFL_POLICIES, PlannedNfl, nfl_accounts
from omni.app.poll_plan import AccountMap, PollPlan
from omni.app.poller import Poller
from omni.app.pipeline import (
    FeedFetcher,
    GameLinesFetcher,
    LiveBaseballPipeline,
    PregameDetailsFetcher,
    WinProbFetcher,
)
from omni.app.quota import (
    BalldontliePlan,
    HttpAnswer,
    Meter,
    MeteredHttp,
    ProviderAccount,
    QuotaLedger,
    nflmeta_billed,
)
from omni.app.supervisor import BackoffPolicy, ProviderSupervisor
from omni.app.trace import DecisionTracer
from omni.app.versions import Versions
from omni.cards.factory import CardFactory
from omni.core.clock import Clock, MonotonicTime, Pacer
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.preferences.favorites import Favorites
from omni.providers.balldontlie import BalldontlieProvider, BdlFetch
from omni.providers.base import PollScope, Provider, ProviderError, ScopeSource
from omni.providers.nba_teams import NbaTeamRegistry
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, NflMetaLiveProvider
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.image import LogoStore
from omni.renderers.registry import RendererRegistry, default_registry
from omni.renderers.text_lane import DEFAULT_MARQUEE, MarqueeStyle
from omni.tower.board import BoardPolicy
from omni.tower.interruption import InterruptionPolicy
from omni.policy.settings import PolicySettings

__all__ = [
    "NflSource",
    "board_plan",
    "build_loop",
    "build_planned_loop",
    "paint_frame",
    "planned_nba_source",
    "planned_nfl_source",
    "run_forever",
    "warm_up",
]

_WARM_UP_PASSES = 3  # the slate, then its games' feeds, then a pass that can show them
_REFRESH_AT_LEAST_EVERY = timedelta(seconds=5)  # for what changes with nothing to wake it (an answer expiring)


@dataclass(frozen=True, slots=True)
class NflSource:
    """NFL on the board: the scope source that hands it NFL's slate, and the feeds that hold its games' answers.

    ``held_back``, when given, says why a quota is holding one of NFL's scopes back, for the league's health.
    """

    schedule: ScopeSource
    feeds: NflFeeds
    held_back: QuotaNote | None = None


def build_loop(
    schedule: Provider | ScopeSource | None,
    fetch_feed: FeedFetcher,
    sink: DisplaySink | AcknowledgingSink,
    *,
    favorites: Favorites = Favorites(),
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
    quota: QuotaNote | None = None,
    nfl: NflSource | None = None,
    nfl_delay: DurationSeconds = DurationSeconds(0),
    settings: PolicySettings | None = None,
    interruptions: InterruptionPolicy | None = None,
) -> AppLoop:
    """Wire the standard spine into an `AppLoop` (deterministic; no I/O performed here).

    ``sink`` is the display: one that shows a frame as it commits it (the emulator), or one that confirms what it
    showed afterward (a board's receiver, `omni.app.frame_stream.StreamDisplaySink`), which books on its word.

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
    `quota` says why a quota holds a scope back, for its league's health.
    `nfl`, when given, puts NFL on the board: its slate beside ``schedule``, and its games carded through the same
    queue, held back for `nfl_delay` (`broadcast_lag` is baseball's). Its favorites are ``favorites``' NFL clubs.
    With no ``schedule`` the board is NFL's alone. `settings` are the board policy's user settings (the defaults
    when left out). `interruptions`, when given, has the loop watch for its host sleeping or stalling, and wake at
    least every health tick (`omni.tower.interruption`); the running board always does.
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
    schedules: tuple[ScopeSource, ...] = (schedule,) if schedule is not None else ()
    football = None
    if nfl is not None:
        schedules += (nfl.schedule,)
        football = LiveFootballPipeline(
            feeds=nfl.feeds, delay_policy=DelayPolicy(broadcast_lag=nfl_delay), queue=queue, favorites=favorites
        )
        quota = _first_note(quota, nfl.held_back)
    return AppLoop(
        schedules=schedules,
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
        quota=quota,
        football=football,
        settings=settings,
        favorites=favorites,
        interruptions=interruptions,
    )


def _first_note(*notes: QuotaNote | None) -> QuotaNote:
    """One quota note asking each given in turn: the first to say a scope is held back says why."""
    asked = [note for note in notes if note is not None]

    def note(scope: PollScope) -> str | None:
        return next((said for said in (each(scope) for each in asked) if said is not None), None)

    return note


def build_planned_loop(
    planned: PlannedMlb | None,
    sink: DisplaySink | AcknowledgingSink,
    *,
    meter: bool = False,
    favorites: Favorites = Favorites(),
    broadcast_lag: DurationSeconds = DurationSeconds(45),
    logos: LogoStore | None = None,
    display_timezone: tzinfo | None = None,
    marquee: MarqueeStyle = DEFAULT_MARQUEE,
    policy: BoardPolicy = BoardPolicy(),
    registry: RendererRegistry | None = None,
    nfl: NflSource | None = None,
    nfl_delay: DurationSeconds = DurationSeconds(0),
    settings: PolicySettings | None = None,
    interruptions: InterruptionPolicy | None = None,
) -> AppLoop:
    """The running MLB board: the slate, feeds, pregame details and odds all read from ``planned``'s plan.

    The live win meter (the market, then MLB's model) is on only with ``meter``. Each reading stands
    beside a score only if it arrived no later than that score's feed answer. ``nfl``, when given, puts NFL
    on the board beside it, held back for ``nfl_delay``; with no ``planned``, the board is NFL's alone.
    ``settings`` are the board policy's user settings, and ``interruptions`` as `build_loop` takes them.
    """
    if planned is None:
        return build_loop(
            None,
            _no_baseball,
            sink,
            favorites=favorites,
            broadcast_lag=broadcast_lag,
            logos=logos,
            display_timezone=display_timezone,
            marquee=marquee,
            policy=policy,
            registry=registry,
            nfl=nfl,
            nfl_delay=nfl_delay,
            settings=settings,
            interruptions=interruptions,
        )
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
        quota=planned.held_back,
        nfl=nfl,
        nfl_delay=nfl_delay,
        settings=settings,
        interruptions=interruptions,
    )


def _no_baseball(game: TeamGame, now: datetime) -> LiveBaseballFeed:
    """A board without MLB has no baseball games, so no feed of one is ever read."""
    raise ProviderError(f"{game.id}: MLB isn't on this board")


def board_plan(
    *, ledger: QuotaLedger | None, mlb: AccountMap | None, nfl: bool, nba: BalldontliePlan | None = None
) -> PollPlan:
    """The one plan the board's leagues ask through: MLB's reads when ``mlb`` names the accounts they spend
    (`omni.app.planned_mlb.mlb_accounts`), NFL's with ``nfl``, every NFL read spending NFLMeta's account, and NBA's
    when ``nba`` names the BALLDONTLIE tier they're paced for, every one spending BALLDONTLIE's account. The
    ``ledger`` admits each ask against its account's quota."""
    if mlb is None and not nfl and nba is None:
        raise ValueError("a board's plan needs at least one league")
    policies = {
        **(MLB_POLICIES if mlb is not None else {}),
        **(NFL_POLICIES if nfl else {}),
        **(nba_policies(nba) if nba is not None else {}),
    }
    sources = {
        **(mlb.sources if mlb is not None else {}),
        **(nfl_accounts().sources if nfl else {}),
        **(nba_accounts().sources if nba is not None else {}),
    }
    kinds = mlb.kinds if mlb is not None else {}
    return PollPlan(policies, ledger=ledger, accounts=AccountMap(sources=sources, kinds=kinds))


def planned_nfl_source(
    plan: PollPlan,
    clock: Clock,
    get: Callable[[str, Mapping[str, str | int]], HttpAnswer],
    *,
    ledger: QuotaLedger,
    favorites: Favorites = Favorites(),
) -> NflSource:
    """NFL on the board: NFLMeta read through ``get`` on ``plan``, each response charged to NFLMeta's account on
    the ``ledger`` (rows and all, from its headers), as the NFL runner reads it."""
    meter = Meter(ledger, ProviderAccount.NFLMETA, clock, billed=nflmeta_billed)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=MeteredHttp(get, meter))
    planned = PlannedNfl(plan, clock, provider, favorites=favorites)
    return NflSource(schedule=planned, feeds=planned, held_back=planned.held_back)


def planned_nba_source(
    plan: PollPlan,
    clock: Clock,
    fetch: BdlFetch,
    *,
    ledger: QuotaLedger,
    tier: BalldontliePlan,
    log: Callable[[str], None] = print,
) -> PlannedNba:
    """NBA on the board: BALLDONTLIE read through ``fetch`` on ``plan`` at ``tier``'s pace, each request charged to
    BALLDONTLIE's account on the ``ledger``, and its word on its minute heeded (`omni.app.planned_nba.MeteredBdl`)."""
    meter = Meter(ledger, ProviderAccount.BALLDONTLIE, clock)
    provider = BalldontlieProvider(NbaTeamRegistry.default(), fetch=MeteredBdl(fetch, meter, clock), clock=clock)
    return PlannedNba(plan, clock, provider, tier=tier, log=log)


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
    versions: Versions | None = None,
) -> None:  # pragma: no cover - real-time loop
    """Refresh on a worker thread whenever there's reason to, and paint `fps` times a second here.

    ``versions``, when given, opens the trace: what the run decides with."""
    tracer = DecisionTracer(log, timezone=timezone)
    if versions is not None:
        tracer.versions(versions)

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
