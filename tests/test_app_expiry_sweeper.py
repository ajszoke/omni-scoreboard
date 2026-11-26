"""The shared app's expiry sweep: at start, on a steady cadence while it runs, and at exit, never stopping the board.

Each test gives the sweeper its clock, its roots and its sweep, so nothing here touches the checkout's own
``build/`` folders.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omni.app.expiry import SWEEP_EVERY, ExpirySweeper, expire
from omni.app.run_log import RUN_LOG_FILE
from omni.core.clock import FakeClock
from omni.providers.rights import DataSource, stamp_recording
from omni.telemetry.miners import NflMetaMiner
from omni.telemetry.runner_log_redact import lines_to_redact
from tests.nflmeta_recording import PHRASE
from tests.test_telemetry_runner_log import NIGHT_ONE

NOW = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)
_SOON = 5.0  # seconds a test waits for the sweeper's thread before calling it stuck


class _Sweeps:
    """A sweep that notes what it was given, and fails when told to."""

    def __init__(self, *, failing: Iterable[int] = (), enough: int = 0) -> None:
        self.calls: list[tuple[tuple[Path, ...], datetime]] = []
        self.failing = frozenset(failing)
        self.enough = enough
        self.reached = threading.Event()

    def __call__(self, roots: Iterable[Path], now: datetime, log: Callable[[str], None]) -> None:
        self.calls.append((tuple(roots), now))
        if self.enough and len(self.calls) >= self.enough:
            self.reached.set()
        if len(self.calls) in self.failing:
            raise RuntimeError(f"PROVIDER PLAY TEXT: {PHRASE}")


def test_the_cadence_is_an_hour() -> None:
    assert SWEEP_EVERY == timedelta(hours=1)


def test_the_sweep_runs_as_the_app_starts_and_as_it_stops_mining_a_run_s_log_before_it_goes(
    tmp_path: Path,
) -> None:
    recordings, logs = tmp_path / "recordings", tmp_path / "logs"
    folder = logs / "2026-11-15-PIT-CIN"
    stamp_recording(folder, DataSource.NFLMETA, NOW - timedelta(days=10))
    (folder / RUN_LOG_FILE).write_text("\n".join(NIGHT_ONE) + "\n")
    telemetry = tmp_path / "telemetry"
    sweep = functools.partial(expire, miners={DataSource.NFLMETA: NflMetaMiner(telemetry)})
    lines: list[str] = []
    sweeper = ExpirySweeper(FakeClock(NOW), lines.append, roots=(recordings, logs), sweep=sweep)
    sweeper.start()
    assert not folder.exists()
    digest = telemetry / "runner-log" / folder.name / "2026-11-15.json"
    assert lines == [
        f"expiry under {logs}: 2026-11-15-PIT-CIN  nflmeta  deleted: due 2026-11-22 21:00 UTC; mined to {digest}"
    ]
    redacted = digest.with_name("2026-11-15.redacted.log").read_text().splitlines()
    assert lines_to_redact(redacted) == 0 and PHRASE not in "\n".join(redacted)
    sweeper.stop()
    assert len(lines) == 1  # nothing more was due: the exit's sweep said nothing


def test_the_sweep_runs_on_its_cadence_until_stopped_with_the_clock_s_time_and_the_roots() -> None:
    sweeps = _Sweeps(enough=4)
    roots = (Path("recordings"), Path("logs"))
    sweeper = ExpirySweeper(FakeClock(NOW), print, roots=roots, every=timedelta(milliseconds=1), sweep=sweeps)
    sweeper.start()
    assert sweeps.reached.wait(_SOON)  # the start's sweep, then the cadence's
    sweeper.stop()
    ran = len(sweeps.calls)
    assert ran >= 5 and set(sweeps.calls) == {(roots, NOW)}  # the exit's sweep, last
    sweeper.stop()  # stopped already: only the sweep
    assert len(sweeps.calls) == ran + 1


def test_a_sweep_that_fails_is_logged_by_its_type_and_the_next_one_still_runs() -> None:
    sweeps = _Sweeps(failing=(1, 2), enough=3)
    lines: list[str] = []
    sweeper = ExpirySweeper(FakeClock(NOW), lines.append, roots=(), every=timedelta(milliseconds=1), sweep=sweeps)
    sweeper.start()  # its own sweep fails, and it starts all the same
    assert sweeps.reached.wait(_SOON)
    sweeper.stop()
    failed = "expiry: the sweep failed (RuntimeError); the next one tries again"
    assert lines == [failed, failed]
    assert len(sweeps.calls) >= 4


def test_a_sweeper_stopped_without_starting_sweeps_once() -> None:
    sweeps = _Sweeps(failing=(1,))
    lines: list[str] = []
    ExpirySweeper(FakeClock(NOW), lines.append, roots=(), sweep=sweeps).stop()
    assert len(sweeps.calls) == 1 and lines == ["expiry: the sweep failed (RuntimeError); the next one tries again"]


@pytest.mark.parametrize("every", [timedelta(0), timedelta(seconds=-1)])
def test_a_sweep_s_cadence_is_positive(every: timedelta) -> None:
    with pytest.raises(ValueError, match="cadence must be positive"):
        ExpirySweeper(FakeClock(NOW), print, every=every)
