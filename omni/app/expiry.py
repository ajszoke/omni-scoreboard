"""The expiry trigger: what the sweep sweeps, which miners it offers each recording to, and what it logs.

A recording or run log of a source whose terms allow no standing copy is stamped to expire, and
`omni.providers.rights.sweep_expired` deletes each one past its delete-by, mining it first. `expire` runs it
over the roots this checkout writes to: ``tools/recordings_due.py --expire`` runs it, the NFL runner at start
and at exit, and the shared app (`ExpirySweeper`) at start, every `SWEEP_EVERY` while it runs, and at exit. Each
source's miner is wired in `MINERS` (NFLMeta's keeps its digests and a run log's redacted nights under
`TELEMETRY`); a source without one is deleted unmined once overdue.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType

from omni.core.clock import Clock
from omni.providers.rights import DataSource, Miner, SweepAction, SweepReport, sweep_expired
from omni.telemetry.miners import NflMetaMiner

__all__ = [
    "EXPIRY_ROOTS",
    "MINERS",
    "NFLMETA_RUN_LOGS",
    "RECORDINGS",
    "SWEEP_EVERY",
    "TELEMETRY",
    "ExpirySweeper",
    "expire",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDINGS = _REPO_ROOT / "build" / "recordings"  # recordings made to diagnose a feed
NFLMETA_RUN_LOGS = _REPO_ROOT / "build" / "logs" / "nflmeta"  # the NFL runner's full logs, a folder a run
EXPIRY_ROOTS = (RECORDINGS, NFLMETA_RUN_LOGS)
TELEMETRY = _REPO_ROOT / "build" / "telemetry"  # our own measurements, mined before a recording expires; kept
MINERS: Mapping[DataSource, Miner] = MappingProxyType({DataSource.NFLMETA: NflMetaMiner(TELEMETRY)})
# How often a run that lasts days sweeps between its start and its exit. A sweep reads only stamps until something
# is due, and a delete-by is days out, so an hour late is nothing.
SWEEP_EVERY = timedelta(hours=1)


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


class ExpirySweeper:
    """The expiry sweep for a run that lasts days: once as it starts (`start`), every ``every`` on a thread of its
    own while it runs, so a sweep that mines never holds up a frame, and once as it stops (`stop`).

    Each sweep runs `expire` over ``roots`` at the clock's time and logs only its own lines, which name folders
    and counts. A sweep that fails is logged by the failure's type and never raised: the board carries on, and
    the next sweep tries again. Sweeps never overlap.
    """

    def __init__(
        self,
        clock: Clock,
        log: Callable[[str], None],
        *,
        roots: Iterable[Path] = EXPIRY_ROOTS,
        every: timedelta = SWEEP_EVERY,
        sweep: Callable[[Iterable[Path], datetime, Callable[[str], None]], object] = expire,
    ) -> None:
        if every <= timedelta(0):
            raise ValueError("a sweep's cadence must be positive")
        self._clock = clock
        self._log = log
        self._roots = tuple(roots)
        self._every = every
        self._sweep = sweep
        self._sweeping = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="expiry", daemon=True)

    def sweep(self) -> None:
        """Sweep once, now."""
        with self._sweeping:
            try:
                self._sweep(self._roots, self._clock.now(), self._log)
            except Exception as exc:  # the sweep's failure is logged; the board it serves carries on
                self._log(f"expiry: the sweep failed ({type(exc).__name__}); the next one tries again")

    def start(self) -> None:
        """Sweep now, then every ``every`` until stopped."""
        self.sweep()
        self._thread.start()

    def stop(self) -> None:
        """Stop the cadence, waiting out a sweep under way, and sweep once more."""
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join()
        self.sweep()

    def _run(self) -> None:
        while not self._stopped.wait(self._every.total_seconds()):
            self.sweep()
