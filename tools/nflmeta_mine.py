"""Mine an NFLMeta recording, or the NFL runner's log, into our own telemetry before the raw data has to go.

    .venv/bin/python tools/nflmeta_mine.py recording build/recordings/nflmeta/2026-11-15-PIT-CIN
    .venv/bin/python tools/nflmeta_mine.py runner-log build/logs/football-live.log --first-date 2026-11-15

A recording becomes ``build/telemetry/nflmeta/<folder>.json``, and the runner's log one
``build/telemetry/runner-log/<date>.json`` a night, each with a Markdown summary beside it (`omni.telemetry`). They
hold only our own measurements (counts, our kinds, spans), never the provider's words, so they may be kept after
the recording and the log are deleted. A digest already written is left as it is. This prints the paths and a few
counts from the digests, nothing from the recording or the log.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly

from omni.telemetry.nflmeta_mine import mine_recording
from omni.telemetry.runner_log_mine import mine_runner_log

_TELEMETRY = _ROOT / "build" / "telemetry"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    kinds = parser.add_subparsers(dest="what", required=True)
    recording = kinds.add_parser("recording", help="mine one NFLMeta recording folder")
    recording.add_argument("folder", type=Path)
    recording.add_argument("--out", type=Path, default=_TELEMETRY / "nflmeta")
    log = kinds.add_parser("runner-log", help="mine the NFL runner's log, a digest per night")
    log.add_argument("log", type=Path)
    log.add_argument("--first-date", type=date.fromisoformat, required=True, help="the date of the log's first night")
    log.add_argument("--out", type=Path, default=_TELEMETRY / "runner-log")
    args = parser.parse_args(argv)
    if args.what == "recording":
        path = mine_recording(args.folder, args.out)
        if path is None:
            print(f"{args.folder.name}: not an NFLMeta recording; nothing written")
            return 1
        digest = json.loads(path.read_text())
        breaks = sum(kind["count"] for kind in digest["breaks"])
        print(
            f"{path}\n{path.with_suffix('.md')}\n{digest['replay']['scoreboards']} scoreboards,"
            f" {digest['latency']['plays']} plays, {breaks} breaks"
        )
        return 0
    for path in mine_runner_log(args.log, args.out, args.first_date):
        digest = json.loads(path.read_text())
        lines = digest["lines"]
        print(f"{path}  {digest['runs']} runs, {lines['parsed']} lines read, {lines['unparsed']} not")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
