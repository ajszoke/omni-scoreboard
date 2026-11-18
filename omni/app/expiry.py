"""The expiry trigger: what the sweep sweeps, which miners it offers each recording to, and what it logs.

A recording or run log of a source whose terms allow no standing copy is stamped to expire, and
`omni.providers.rights.sweep_expired` deletes each one past its delete-by, mining it first. `expire` runs it
over the roots this checkout writes to: ``tools/recordings_due.py --expire`` runs it, and so does the NFL
runner at start and at exit. Each source's miner is wired in `MINERS`; a source without one is deleted
unmined once overdue.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from omni.providers.rights import DataSource, Miner, SweepAction, SweepReport, sweep_expired

__all__ = ["EXPIRY_ROOTS", "MINERS", "NFLMETA_RUN_LOGS", "RECORDINGS", "expire"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDINGS = _REPO_ROOT / "build" / "recordings"  # recordings made to diagnose a feed
NFLMETA_RUN_LOGS = _REPO_ROOT / "build" / "logs" / "nflmeta"  # the NFL runner's full logs, a folder a run
EXPIRY_ROOTS = (RECORDINGS, NFLMETA_RUN_LOGS)
MINERS: Mapping[DataSource, Miner] = MappingProxyType({})  # each source's miner, by its source


def expire(
    roots: Iterable[Path],
    now: datetime,
    log: Callable[[str], None],
    *,
    miners: Mapping[DataSource, Miner] = MINERS,
    remove: Callable[[Path], None] = shutil.rmtree,
) -> tuple[SweepReport, ...]:
    """Sweep each root at ``now``, logging a line for each recording deleted, or that failed to delete; the
    sweeps' reports. A line names the recording and counts; never its content."""
    reports = tuple(sweep_expired(root, now, miners=miners, remove=remove) for root in roots)
    for report in reports:
        for swept in report.recordings:
            if swept.action is not SweepAction.KEPT:
                log(f"expiry under {report.root}: {swept.line()}")
    return reports
