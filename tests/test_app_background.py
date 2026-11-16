"""Work beside the frame loop: `Repeating` runs on its own thread, and survives its own failures."""

from __future__ import annotations

import threading
from datetime import timedelta

from omni.app.background import Repeating, Waking
from omni.core.clock import MonotonicTime, SystemClock

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


def test_a_waking_worker_runs_at_once_again_when_woken_and_by_its_longest_wait() -> None:
    runs: list[int] = []
    second, third = threading.Event(), threading.Event()

    def action() -> MonotonicTime | None:
        runs.append(len(runs))
        if len(runs) == 2:
            second.set()
        if len(runs) == 3:
            third.set()
        return None  # no deadline of its own

    worker = Waking("test", SystemClock(), action, at_least_every=timedelta(seconds=60), on_error=lambda exc: None)
    worker.start()
    worker.wake()  # whether or not the first run has finished, a second follows
    assert second.wait(_SOON)
    worker.wake()
    assert third.wait(_SOON)
    worker.stop(_SOON)
    assert not worker.running and runs[:3] == [0, 1, 2]


def test_a_waking_worker_runs_again_at_its_deadline_and_survives_a_failure() -> None:
    clock = SystemClock()
    errors: list[str] = []
    again = threading.Event()

    def action() -> MonotonicTime | None:
        if not errors:
            errors.append("boom")
            raise RuntimeError("boom")
        again.set()
        return clock.monotonic()  # due again already: it runs again without waiting

    failing = Waking("test", clock, action, at_least_every=timedelta(milliseconds=5), on_error=lambda exc: None)
    failing.start()
    assert again.wait(_SOON)  # the next run came after the longest wait, failure or not
    failing.stop(_SOON)
    assert errors == ["boom"]


def test_a_waking_worker_needs_a_positive_longest_wait_and_can_stop_unstarted() -> None:
    import pytest

    with pytest.raises(ValueError, match="positive"):
        Waking("test", SystemClock(), lambda: None, at_least_every=timedelta(0), on_error=lambda exc: None)
    worker = Waking("test", SystemClock(), lambda: None, at_least_every=timedelta(seconds=1), on_error=lambda exc: None)
    worker.stop()
    assert not worker.running
