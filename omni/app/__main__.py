"""``python -m omni.app`` — run the live scoreboard on the RGBMatrixEmulator.

    python -m omni.app --emulated --profile quad_128x64 --favorite COL --favorite LAD

Polls the MLB StatsAPI, runs the delay-safe pipeline, and rotates cards on the
emulator (browser at http://localhost:8888/). This is the thin I/O shell around the
deterministic `AppLoop`; everything testable lives in `runner.py` / `loop.py`.
"""

from __future__ import annotations

import argparse
from typing import Sequence, cast

from datetime import datetime

from omni.app.clock import SystemClock
from omni.app.display import MatrixDevice, MatrixDisplaySink
from omni.app.runner import build_loop, run_forever
from omni.core.enum import PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.panels.geometry import geometry_for
from omni.renderers.image import LogoStore

__all__ = ["main"]


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
    parser.add_argument("--tick", type=int, default=12, help="seconds between display refreshes (default 12)")
    parser.add_argument(
        "--timezone",
        default="America/New_York",
        metavar="IANA",
        help="IANA zone for the schedule day, e.g. America/Denver (default America/New_York)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - drives the live emulator + network
    args = _parse_args(argv)
    profile = PanelProfile(args.profile)

    # Lazy imports keep the network library and emulator out of import time and tests.
    import statsapi  # noqa: F401  (ensures the dependency is present before we start)

    from zoneinfo import ZoneInfo

    from omni.providers.mlb_statsapi import MlbStatsApiProvider
    from omni.providers.mlb_teams import MlbTeamRegistry

    provider = MlbStatsApiProvider(MlbTeamRegistry.from_color_file(), schedule_timezone=ZoneInfo(args.timezone))

    def fetch_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        return provider.fetch_live_feed(game, now=now)

    def fetch_win_probability(game: TeamGame) -> WinProbability | None:
        return provider.fetch_win_probability(game)

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
        provider,
        fetch_feed,
        sink,
        favorites=frozenset(args.favorite),
        broadcast_lag=DurationSeconds(args.delay),
        fetch_win_probability=fetch_win_probability,  # per-team meter, delayed in lockstep with the score
        logos=LogoStore(),  # resolve committed team tiles from assets/ (lazy, cached)
    )
    print(f"omni.app: {profile.value}, delay {args.delay}s, favorites {sorted(args.favorite)} — http://localhost:8888/")
    run_forever(loop, SystemClock(), tick=DurationSeconds(args.tick))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
