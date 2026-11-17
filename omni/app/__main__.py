"""``python -m omni.app`` — run the live scoreboard on the RGBMatrixEmulator.

    python -m omni.app --emulated --profile quad_128x64 --favorite mlb:ATL --favorite mlb:COL

Polls the MLB StatsAPI, runs the delay-safe pipeline, and rotates cards on the
emulator (browser at http://localhost:8888/). This is the thin I/O shell around the
deterministic `AppLoop`; everything testable lives in `runner.py` / `loop.py`.
Each card stays up for its turn (``--slot``, held within the card's own minimum and maximum)
unless a bigger moment takes over, and frames draw ``--fps`` times a second, so scrolling lines
and countdowns move. Each change on the board is traced. The network is read by a poller on its
own threads, on the cadences in `omni.app.planned_mlb.MLB_POLICIES`, pregame details and odds
included; a refresh reads its latest answers whenever one lands, and never waits on a request.

Each ``--favorite`` is a league and a team, and their order is the rank within each league: above, ATL
is MLB's first and COL its second. A bare team (``--favorite COL``) is read as MLB's, the only league
the flag once meant, and the app says so as it starts. An unknown league or team stops it with a usage
error that names the entry.

The trace opens with what the run decides with (`omni.app.versions`): the board policy, a digest of the
settings that change its decisions (spelled out on the line under it), the rule table, and the commit.

The live win meter is off unless ``--meter`` is given. With it, and with ``OMNI_CLOUD_URL`` and
``OMNI_CLOUD_TOKEN`` in the environment or a git-ignored ``.env``, the meter reads Omni's market
service (``cloud/``); without them it reads Polymarket's public quotes. Either way MLB's model takes
over when the market has nothing.

Every request is counted against its provider account's quota (`omni.app.quota`), and the month's counts
are kept under ``--state-dir`` so a restart doesn't forget them. ``--nflmeta-plan`` names the plan of the
NFLMeta key, free unless you say otherwise.
"""

from __future__ import annotations

import argparse
import os
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence, cast
from zoneinfo import ZoneInfo

from omni.core.clock import SystemClock
from omni.app.display import MatrixDevice, MatrixDisplaySink
from omni.app.planned_mlb import MarketReader
from omni.app.quota import Meter, NflMetaPlan, ProviderAccount
from omni.app.runner import build_planned_loop, run_forever
from omni.app.versions import DecisionConfig, PolicyName, SourceRevision, Versions, read_source_revision
from omni.core.enum import PanelProfile
from omni.core.env import read_env_file
from omni.core.time import DurationSeconds
from omni.domain.rules import RULES
from omni.panels.geometry import geometry_for
from omni.preferences.favorites import AppliedMigration, parse_favorites
from omni.renderers.image import LogoStore
from omni.renderers.text_lane import DEFAULT_MARQUEE, ScrollSpeed
from omni.tower.board import BoardPolicy

if TYPE_CHECKING:
    from omni.providers.market_service import ServiceTransport

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="omni.app", description="Run the Omni scoreboard loop.")
    parser.add_argument("--profile", required=True, choices=[profile.value for profile in PanelProfile])
    parser.add_argument(
        "--emulated", action="store_true", help="render on the RGBMatrixEmulator (the only target today)"
    )
    parser.add_argument(
        "--favorite",
        action="append",
        default=[],
        metavar="LEAGUE:ABBR",
        help="a favorite team, e.g. mlb:ATL or nfl:CAR (repeatable; the order is the rank, and a bare ABBR is MLB)",
    )
    parser.add_argument("--delay", type=int, default=45, help="TV broadcast delay in seconds (default 45)")
    parser.add_argument(
        "--slot",
        "--tick",
        dest="slot",
        type=int,
        default=12,
        help="seconds a card in rotation stays up when another is waiting, within its own minimum and maximum"
        " (default 12)",
    )
    parser.add_argument(
        "--timezone",
        default="America/New_York",
        metavar="IANA",
        help="IANA zone for the schedule day and board clocks, e.g. America/Denver (default America/New_York)",
    )
    parser.add_argument("--no-odds", action="store_true", help="leave sportsbook odds off the pregame card")
    parser.add_argument(
        "--meter",
        action="store_true",
        help="show the live win meter (the market, then MLB's model); off by default",
    )
    parser.add_argument(
        "--scroll-speed",
        choices=["slow", "medium", "fast"],
        default="slow",
        help="how fast scrolling lines move at their peak: slow, medium (1.25x) or fast (1.5x)",
    )
    parser.add_argument("--fps", type=float, default=10.0, help="frames drawn a second (marquees move this fast)")
    parser.add_argument(
        "--nflmeta-plan",
        choices=[plan.value for plan in NflMetaPlan],
        default=NflMetaPlan.FREE.value,
        help="the NFLMeta plan the key is on, whose limits the board keeps to (default free)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="where the month's quota usage is kept (default $XDG_STATE_HOME/omni, else ~/.local/state/omni)",
    )
    args = parser.parse_args(argv)
    migrations: list[AppliedMigration] = []

    def migrated(applied: AppliedMigration) -> None:
        migrations.append(applied)
        print(f"omni.app: {applied}")

    try:
        args.favorites = parse_favorites(args.favorite, note=migrated)
    except ValueError as exc:
        parser.error(str(exc))
    args.migrations = tuple(migrations)
    return args


def _versions(args: argparse.Namespace, source: SourceRevision) -> Versions:
    """What a run with these flags decides with. The display target and the frame rate aren't settings
    that change a decision, so they're left out."""
    board = _board_policy(args)
    return Versions(
        policy=PolicyName.BOARD_V1,
        config=DecisionConfig(
            profile=PanelProfile(args.profile),
            delay=timedelta(seconds=args.delay),
            favorites=args.favorites,
            turn=board.slot,
            takeover_cap=board.takeover_cap,
            timezone=ZoneInfo(args.timezone),
            meter=args.meter,
            odds=not args.no_odds,
            scroll_speed=ScrollSpeed(args.scroll_speed),
        ),
        rules=RULES,
        source=source,
        migrations=args.migrations,
    )


def _board_policy(args: argparse.Namespace) -> BoardPolicy:
    return BoardPolicy(slot=timedelta(seconds=args.slot))


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - drives the live emulator + network
    args = _parse_args(argv)
    profile = PanelProfile(args.profile)

    # Lazy imports keep the network library and emulator out of import time and tests.
    import statsapi  # noqa: F401  (ensures the dependency is present before we start)

    from omni.providers.bovada_odds import BovadaOddsProvider
    from omni.providers.market_service import (
        HeartbeatSender,
        MarketServiceClient,
        MarketServiceConfig,
        UrllibTransport,
    )
    from omni.providers.mlb_statsapi import MlbStatsApiProvider
    from omni.providers.mlb_teams import MlbTeamRegistry
    from omni.providers.polymarket_us import PolymarketUsProvider

    from omni.app.quota_store import default_state_dir, open_ledger
    from omni.providers import bovada_odds
    from omni.providers.polymarket_us import fetch_market_data

    clock = SystemClock()
    state_dir = args.state_dir if args.state_dir is not None else default_state_dir(os.environ, Path.home())
    ledger = open_ledger(state_dir, NflMetaPlan(args.nflmeta_plan), log=lambda line: print(line, flush=True))

    zone = ZoneInfo(args.timezone)
    source = read_source_revision(_REPO_ROOT)
    provider = MlbStatsApiProvider(MlbTeamRegistry.from_color_file(), schedule_timezone=zone)
    # The sportsbook's coupon fetch, each request counted (the provider refreshes it at its own pace).
    sportsbook = Meter(ledger, ProviderAccount.BOVADA, clock).counted(bovada_odds._default_fetch)
    odds = None if args.no_odds else BovadaOddsProvider(fetch=sportsbook)

    # With the meter on, the shared market service when this board has a token for it, else the public quotes.
    market: MarketReader | None = None
    market_account = ProviderAccount.POLYMARKET_US
    meter = "off"
    if args.meter:
        cloud = MarketServiceConfig.from_env({**read_env_file(_REPO_ROOT / ".env"), **os.environ})
        if cloud is not None:
            transport = UrllibTransport(cloud)
            HeartbeatSender(transport, version=str(source)).start()
            service = _CountedTransport(transport, Meter(ledger, ProviderAccount.MARKET_SERVICE, clock))
            market, meter = MarketServiceClient(service).win_probability, "market service"
            market_account = ProviderAccount.MARKET_SERVICE
        else:
            quotes = Meter(ledger, ProviderAccount.POLYMARKET_US, clock).counted(fetch_market_data)
            market, meter = PolymarketUsProvider(fetch=quotes).win_probability, "Polymarket public quotes"

    from omni.app.planned_mlb import MLB_POLICIES, PlannedMlb, mlb_accounts
    from omni.app.poll_plan import PollPlan
    from omni.app.poller import Poller

    plan = PollPlan(MLB_POLICIES, ledger=ledger, accounts=mlb_accounts(provider.source, market=market_account))
    planned = PlannedMlb(
        plan,
        clock,
        provider,
        market=market,
        odds=odds.lines_for if odds is not None else None,
        favorites=args.favorites,
        meter=Meter(ledger, ProviderAccount.MLB_STATSAPI, clock),
    )

    from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions

    options = RGBMatrixOptions()
    width, height = geometry_for(profile).size
    options.cols, options.rows = width, height
    options.chain_length = 1
    options.parallel = 1
    matrix = RGBMatrix(options=options)

    # The emulator's RGBMatrix is the matrix API at runtime, but its SwapOnVSync is typed with
    # the emulator's own canvas type and extra args — broader than our minimal MatrixDevice
    # protocol. Assert the boundary match precisely instead of blanket-ignoring the whole call.
    sink = MatrixDisplaySink(cast(MatrixDevice, matrix), profile)
    loop = build_planned_loop(
        planned,
        sink,
        meter=args.meter,
        favorites=args.favorites,
        broadcast_lag=DurationSeconds(args.delay),
        logos=LogoStore(),  # resolve committed team tiles from assets/ (lazy, cached)
        display_timezone=zone,
        marquee=DEFAULT_MARQUEE.at(ScrollSpeed(args.scroll_speed)),
        policy=_board_policy(args),
    )
    print(
        f"omni.app: {profile.value}, delay {args.delay}s, favorites {args.favorites}, meter {meter}"
        " — http://localhost:8888/"
    )
    try:
        run_forever(
            loop,
            clock,
            fps=args.fps,
            log=lambda line: print(line, flush=True),
            timezone=zone,
            poller=Poller(plan, clock),
            versions=_versions(args, source),
        )
    except KeyboardInterrupt:
        pass
    finally:
        ledger.save(clock.read(), force=True)  # the month's usage, for the next run
    return 0


class _CountedTransport:  # pragma: no cover - wraps the real network
    """The market service's transport, each request it makes counted against the service's account."""

    def __init__(self, transport: ServiceTransport, meter: Meter) -> None:
        self._transport = transport
        self._meter = meter

    def get(self, path: str, *, timeout_s: float) -> Any:
        return self._meter.counted(self._transport.get)(path, timeout_s=timeout_s)

    def post(self, path: str, body: Mapping[str, Any], *, timeout_s: float) -> Any:
        return self._meter.counted(self._transport.post)(path, body, timeout_s=timeout_s)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
