"""List the recordings under ``build/recordings/`` with their source and delete-by date; deletes nothing.

    .venv/bin/python tools/recordings_due.py [--root build/recordings]

Each recording folder carries a ``RETENTION.json`` stamp from when it was made (`omni.providers.rights`).
This prints each folder's name and what its stamp says, marking the ones past their delete-by date as
OVERDUE. A folder without a stamp is listed as unstamped, to follow its source's rule by hand. It reads
the stamps and nothing else, so it never prints a recording's content. Deleting is the user's step.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly

from omni.providers.rights import DueStatus, recordings_due


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=_ROOT / "build" / "recordings", help="the recordings folder")
    args = parser.parse_args(argv)
    due = recordings_due(args.root, now if now is not None else datetime.now(timezone.utc))
    for recording in due:
        print(recording.line())
    overdue = sum(1 for recording in due if recording.status is DueStatus.OVERDUE)
    unstamped = sum(1 for recording in due if recording.status in (DueStatus.UNSTAMPED, DueStatus.UNREADABLE))
    print(f"{len(due)} recordings under {args.root}: {overdue} overdue, {unstamped} without a readable stamp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
