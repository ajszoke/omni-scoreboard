"""``python -m omni.app`` — run the live scoreboard on the RGBMatrixEmulator.

    python -m omni.app --emulated --profile quad_128x64 --favorite COL --favorite LAD

Polls the MLB StatsAPI, runs the delay-safe pipeline, and rotates cards on the
emulator (browser at http://localhost:8888/). This is the thin I/O shell around the
deterministic `AppLoop`; everything testable lives in `runner.py` / `loop.py`.
Each card stays up for its turn (``--slot``, held within the card's own minimum and maximum)
unless a bigger moment takes over, and frames draw ``--fps`` times a second, so scrolling lines
and countdowns move. Each change on the board is traced. The network is read by a poller on its
own threads, on the cadences in `omni.app.planned_mlb.MLB_POLICIES`; a refresh reads its latest
answers whenever one lands.

With ``OMNI_CLOUD_URL`` and ``OMNI_CLOUD_TOKEN`` in the environment or a git-ignored ``.env``, the
win meter reads Omni's market service (``cloud/``); without them it reads Polymarket's public quotes.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Sequence, cast

from omni.core.clock import SystemClock
from omni.app.display import MatrixDevice, MatrixDisplaySink
from omni.app.runner import build_loop, run_forever
from omni.core.enum import PanelProfile
from omni.core.env import read_env_file
from omni.core.time import DurationSeconds
from omni.panels.geometry import geometry_for
from omni.renderers.image import LogoStore
from omni.renderers.text_lane import DEFAULT_MARQUEE, ScrollSpeed
from omni.tower.board import BoardPolicy

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="omni.app", description="Run the Omni scoreboard loop.")
    parser.add_argument("--profile", required=True, choices=[profile.value for profile in PanelProfile])
    parser.add_argument(
        "--emulated", action="store_true", help="render on the RGBMatrixEmulator (the only target today)"
    )
    parser.add_argument(
        "--favorite", action="append", default=[], metavar="ABBR", help="favorite team abbreviation (repeatable)"
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
        "--scroll-speed",
        choices=["slow", "medium", "fast"],
        default="slow",
        help="how fast scrolling lines move at their peak: slow, medium (1.25x) or fast (1.5x)",
    )
    parser.add_argument("--fps", type=float, default=10.0, help="frames drawn a second (marquees move this fast)")
    return parser.parse_args(argv)


def _app_version() -> str:  # pragma: no cover - reads the checkout
    """The checked-out commit, which is what a heartbeat reports."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - drives the live emulator + network
    args = _parse_args(argv)
    profile = PanelProfile(args.profile)

    # Lazy imports keep the network library and emulator out of import time and tests.
    import statsapi  # noqa: F401  (ensures the dependency is present before we start)

    from zoneinfo import ZoneInfo

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

    zone = ZoneInfo(args.timezone)
    provider = MlbStatsApiProvider(MlbTeamRegistry.from_color_file(), schedule_timezone=zone)
    odds = None if args.no_odds else BovadaOddsProvider()

    # The shared market service when this board has a token for it; the public quotes otherwise.
    cloud = MarketServiceConfig.from_env({**read_env_file(_REPO_ROOT / ".env"), **os.environ})
    if cloud is not None:
        transport = UrllibTransport(cloud)
        HeartbeatSender(transport, version=_app_version()).start()
        fetch_market_win_probability = MarketServiceClient(transport).win_probability
        meter = "market service"
    else:
        fetch_market_win_probability = PolymarketUsProvider().win_probability
        meter = "Polymarket public quotes"

    from omni.app.planned_mlb import MLB_POLICIES, NO_BACKOFF, PlannedMlb
    from omni.app.poll_plan import PollPlan
    from omni.app.poller import Poller

    clock = SystemClock()
    plan = PollPlan(MLB_POLICIES)
    planned = PlannedMlb(plan, clock, provider, market=fetch_market_win_probability, favorites=frozenset(args.favorite))

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
    loop = build_loop(
        planned,  # the slate, from the plan
        planned.fetch_feed,
        sink,
        favorites=frozenset(args.favorite),
        broadcast_lag=DurationSeconds(args.delay),
        backoff=NO_BACKOFF,  # the plan does the retrying
        fetch_win_probability=planned.fetch_win_probability,  # per-team meter, delayed with the score
        fetch_fallback_win_probability=planned.fetch_fallback_win_probability,  # when the market goes quiet
        fetch_pregame_details=provider.fetch_pregame_details,
        fetch_game_lines=odds.lines_for if odds is not None else None,
        logos=LogoStore(),  # resolve committed team tiles from assets/ (lazy, cached)
        display_timezone=zone,
        marquee=DEFAULT_MARQUEE.at(ScrollSpeed(args.scroll_speed)),
        policy=BoardPolicy(slot=timedelta(seconds=args.slot)),
    )
    print(
        f"omni.app: {profile.value}, delay {args.delay}s, favorites {sorted(args.favorite)}, meter {meter}"
        " — http://localhost:8888/"
    )
    run_forever(
        loop,
        clock,
        fps=args.fps,
        log=lambda line: print(line, flush=True),
        timezone=zone,
        poller=Poller(plan, clock),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
