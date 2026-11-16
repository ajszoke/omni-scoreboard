"""``python -m omni.app.football`` — follow one live NFL game on a board.

    python -m omni.app.football --profile quad_128x64 --teams PIT,CIN \\
        --sink-command "ssh omni-board sudo python3 omni/board_frame_sink.py"

Reads NFLMeta with ``NFLMETA_API_KEY`` (environment or the git-ignored ``.env``); ``--odds`` adds the win
meter from Polymarket US's public quotes (off by default: see `omni.app.football_board`). Frames go to
the display process the sink command starts, which is restarted if it drops; ``--emulated`` draws on the
RGBMatrixEmulator instead. The feed is read on one thread and frames are written on another, so neither
a slow request nor a stalled display holds up drawing. ``--record`` appends every NFLMeta response to a recording under
``build/recordings/nflmeta/``, for diagnosing the feed; NFLMeta's terms allow ordinary caching but not
a standing copy of its data, so keep recordings to what a diagnosis needs and delete them after.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Any, cast

from omni.app.clock import Pacer, SystemClock
from omni.app.football_board import FootballBoard
from omni.app.football_feed import BackgroundFeed, GameFeed
from omni.core.enum import PanelProfile
from omni.core.env import read_env_file
from omni.providers.nflmeta import PlayFeedPace
from omni.renderers.text_lane import DEFAULT_MARQUEE, ScrollSpeed

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASE_URL = "https://nflmeta.org"
_RESPAWN_AFTER = 5.0  # seconds before restarting a display process that dropped


class NflMetaHttp:  # pragma: no cover - real network
    """Reads NFLMeta, appending each response to a recording when given a folder for one."""

    def __init__(self, key: str, record_dir: Path | None) -> None:
        self._key = key
        self._record_dir = record_dir
        if record_dir is not None:
            record_dir.mkdir(parents=True, exist_ok=True)

    def get(self, path: str, query: Mapping[str, str | int]) -> Any:
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        url = _BASE_URL + path + (f"?{urlencode(dict(query))}" if query else "")
        request = Request(url, headers={"X-NFLMeta-Key": self._key, "User-Agent": "omni-scoreboard"})
        with urlopen(request, timeout=10) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            body = json.load(response)
        if self._record_dir is not None:
            name = "play-changes.jsonl" if "/play-changes" in path else "live-scores.jsonl"
            line = {
                "received_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "rows": headers.get("x-nflmeta-response-rows"),
                "quota_remaining": headers.get("x-monthly-quota-remaining"),
                "query": dict(query),
                "body": body,
            }
            with open(self._record_dir / name, "a", encoding="utf-8") as out:
                out.write(json.dumps(line) + "\n")
        return body


class SinkProcess:  # pragma: no cover - drives a child process
    """A display process fed frames on its stdin, restarted a few seconds after it drops."""

    def __init__(self, command: Sequence[str]) -> None:
        self._command = list(command)
        self._process: subprocess.Popen[bytes] | None = None
        self._retry_at = 0.0
        self._started = 0  # display processes started so far

    def generation(self) -> int:
        """Which display process is current; a dropped one counts as a new one coming."""
        if self._process is None or self._process.poll() is not None:
            return -1  # none running: the next frame starts one, and goes out whole
        return self._started

    def write(self, frame: bytes) -> bool:
        if self._process is None or self._process.poll() is not None:
            if time.monotonic() < self._retry_at:
                return False
            print(f"display: starting {shlex.join(self._command)}", flush=True)
            self._process = subprocess.Popen(self._command, stdin=subprocess.PIPE)
            self._started += 1
        try:
            stdin = cast(IO[bytes], self._process.stdin)
            stdin.write(frame)
            stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            print("display: lost the display process; restarting shortly", flush=True)
            self._process = None
            self._retry_at = time.monotonic() + _RESPAWN_AFTER
            return False

    def close(self) -> None:
        if self._process is not None and self._process.stdin is not None:
            self._process.stdin.close()
            self._process.wait(timeout=10)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="omni.app.football", description="Follow one live NFL game on a board.")
    parser.add_argument("--profile", required=True, choices=[profile.value for profile in PanelProfile])
    parser.add_argument("--teams", required=True, metavar="AWAY,HOME", help="the game, e.g. PIT,CIN")
    display = parser.add_mutually_exclusive_group(required=True)
    display.add_argument("--sink-command", help="command whose stdin takes the frames (see board_frame_sink.py)")
    display.add_argument("--emulated", action="store_true", help="draw on the RGBMatrixEmulator")
    parser.add_argument("--delay", type=int, default=0, help="seconds to hold the feed back to a broadcast")
    parser.add_argument(
        "--meter-lag", type=int, default=15, help="seconds the win meter trails the market, to match the feed"
    )
    parser.add_argument(
        "--halftime-hold",
        type=int,
        default=None,
        metavar="SECONDS",
        help="how long to stop polling once a game reaches halftime (default 15 minutes)",
    )
    parser.add_argument(
        "--scroll-speed",
        choices=[speed.value for speed in ScrollSpeed],
        default=ScrollSpeed.SLOW.value,
        help="how fast scrolling lines move at their peak: slow, medium (1.25x) or fast (1.5x)",
    )
    parser.add_argument("--fps", type=float, default=10.0, help="frames drawn a second (marquees move this fast)")
    parser.add_argument("--timezone", default="America/New_York", metavar="IANA")
    parser.add_argument(
        "--play-feed",
        choices=[pace.value for pace in PlayFeedPace],
        default=PlayFeedPace.ON_CHANGE.value,
        help="read the play feed when the scoreboard flags a new play, or every poll (about twice the requests)",
    )
    parser.add_argument("--odds", action="store_true", help="show the win meter from the market (experimental)")
    parser.add_argument("--record", action="store_true", help="keep the NFLMeta responses, to diagnose the feed")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - drives the network and a display
    args = _parse_args(argv)
    profile = PanelProfile(args.profile)

    from zoneinfo import ZoneInfo

    from omni.app.display import DisplaySink, MatrixDevice, MatrixDisplaySink
    from omni.app.frame_stream import BackgroundFrameWriter, StreamDisplaySink
    from omni.panels.geometry import geometry_for
    from omni.providers.nfl_teams import NflTeamRegistry
    from omni.providers.nflmeta import HALFTIME_HOLD, NFLMETA_SOURCE, NflMetaLiveProvider
    from omni.providers.polymarket_us import PolymarketUsProvider
    from omni.renderers.image import LogoStore

    key = {**read_env_file(_REPO_ROOT / ".env"), **os.environ}.get("NFLMETA_API_KEY")
    if not key:
        raise SystemExit("omni.app.football: set NFLMETA_API_KEY in the environment or .env")
    away, _, home = args.teams.upper().partition(",")
    record_dir = None
    if args.record:
        day = datetime.now().strftime("%Y-%m-%d")
        record_dir = _REPO_ROOT / "build" / "recordings" / "nflmeta" / f"{day}-{away}-{home}"
    hold = timedelta(seconds=args.halftime_hold) if args.halftime_hold is not None else HALFTIME_HOLD
    provider = NflMetaLiveProvider(
        NflTeamRegistry.default(source=NFLMETA_SOURCE),
        fetch=NflMetaHttp(key, record_dir).get,
        halftime_hold=hold,
        play_feed=PlayFeedPace(args.play_feed),
    )
    game_id = provider.find_game(away, home)
    if game_id is None:
        raise SystemExit(f"omni.app.football: no game today between {away} and {home}")

    clock = SystemClock()
    sink: DisplaySink
    pipe: SinkProcess | None = None
    writer: BackgroundFrameWriter | None = None
    if args.emulated:
        from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions

        options = RGBMatrixOptions()
        options.cols, options.rows = geometry_for(profile).size
        sink = MatrixDisplaySink(cast(MatrixDevice, RGBMatrix(options=options)), profile)
    else:
        pipe = SinkProcess(shlex.split(args.sink_command))
        writer = BackgroundFrameWriter(pipe.write)
        sink = StreamDisplaySink(profile, writer, pipe.generation)

    market = PolymarketUsProvider().win_probability if args.odds else None
    feed = BackgroundFeed(GameFeed(provider, game_id, win_probability=market), clock)
    board = FootballBoard(
        feed,
        sink,
        delay=timedelta(seconds=args.delay),
        meter_lag=timedelta(seconds=args.meter_lag),
        logos=LogoStore(),
        timezone=ZoneInfo(args.timezone),
        marquee=DEFAULT_MARQUEE.at(ScrollSpeed(args.scroll_speed)),
        log=lambda line: print(f"{clock.now().astimezone():%H:%M:%S}  {line}", flush=True),
    )
    print(f"omni.app.football: {away} @ {home} (game {game_id}), {profile.value}, delay {args.delay}s", flush=True)
    if writer is not None:
        writer.start()
    feed.start()
    frames = Pacer(clock, timedelta(seconds=1.0 / args.fps))
    try:
        while True:
            board.step(clock.read())
            frames.wait()
    except KeyboardInterrupt:
        pass
    finally:
        feed.stop(timeout=10)
        if writer is not None:
            writer.stop(timeout=5)
        if pipe is not None:
            pipe.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
