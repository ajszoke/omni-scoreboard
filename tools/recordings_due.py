"""List the stamped recordings and run logs with their source and delete-by date; with ``--expire``, delete
the overdue ones.

    .venv/bin/python tools/recordings_due.py [--root build/recordings] [--root build/logs/nflmeta] [--expire]

Each recording folder (and each NFL run's log folder) carries a ``RETENTION.json`` stamp from when it was
made (`omni.providers.rights`). This prints each folder's name and what its stamp says, marking the ones
past their delete-by date as OVERDUE. A folder without a stamp is listed as unstamped, to follow its
source's rule by hand. ``--expire`` deletes each overdue stamped folder, after offering it to its source's
miner (`omni.app.expiry`), and never an unstamped one or one whose terms allow keeping it. It reads the
stamps and nothing else, so it never prints a recording's content. By default it looks under both roots.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly

from omni.app.expiry import EXPIRY_ROOTS, MINERS
from omni.providers.rights import DataSource, DueStatus, Miner, recordings_due, sweep_expired


def main(
    argv: Sequence[str] | None = None, *, now: datetime | None = None, miners: Mapping[DataSource, Miner] = MINERS
) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--root", type=Path, action="append", help="a folder of stamped recordings (repeatable; default both)"
    )
    parser.add_argument("--expire", action="store_true", help="delete the overdue stamped recordings, mined first")
    args = parser.parse_args(argv)
    at = now if now is not None else datetime.now(timezone.utc)
    for root in args.root or EXPIRY_ROOTS:
        if args.expire:
            report = sweep_expired(root, at, miners=miners)
            for swept in report.recordings:
                print(swept.line())
            print(report.summary())
            continue
        due = recordings_due(root, at)
        for recording in due:
            print(recording.line())
        overdue = sum(1 for recording in due if recording.status is DueStatus.OVERDUE)
        unstamped = sum(1 for recording in due if recording.stamp is None)
        print(f"{len(due)} recordings under {root}: {overdue} overdue, {unstamped} without a readable stamp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
