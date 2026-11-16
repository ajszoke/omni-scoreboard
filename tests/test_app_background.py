"""Work beside the frame loop: `Repeating` runs on its own thread, and survives its own failures."""

from __future__ import annotations

import threading
from datetime import timedelta

from omni.app.background import Repeating
from omni.app.clock import SystemClock

_SOON = 5.0  # seconds a test waits for a thread before calling it stuck


def test_repeats_until_stopped() -> None:
    runs: list[int] = []
    third = threading.Event()

    def action() -> None:
        runs.append(len(runs))
        if len(runs) == 3:
            third.set()

    worker = Repeating("test", SystemClock(), timedelta(milliseconds=1), action, on_error=lambda exc: None)
    worker.start()
    assert third.wait(_SOON)
    worker.stop(_SOON)
    assert not worker.running
    assert runs[:3] == [0, 1, 2]


def test_a_failing_run_is_reported_and_the_beat_goes_on() -> None:
    errors: list[str] = []
    recovered = threading.Event()

    def action() -> None:
        if not errors:
            raise RuntimeError("boom")
        recovered.set()

    worker = Repeating(
        "test", SystemClock(), timedelta(milliseconds=1), action, on_error=lambda exc: errors.append(str(exc))
    )
    worker.start()
    assert recovered.wait(_SOON)
    worker.stop(_SOON)
    assert errors == ["boom"]


def test_stopping_a_worker_that_never_started_is_harmless() -> None:
    worker = Repeating("test", SystemClock(), timedelta(seconds=1), lambda: None, on_error=lambda exc: None)
    worker.stop()
    assert not worker.running
