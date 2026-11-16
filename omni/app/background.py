"""Work that runs beside the frame loop, so drawing never waits on it.

A board draws at a frame rate, while its other work runs at its own pace and can stall: a feed request
can take seconds, and a display across SSH can stop reading. `Repeating` runs such work on its own
daemon thread, once a beat on the clock's monotonic reading, and reports a failure instead of dying
with it. Whatever the work produces reaches the frame loop through an immutable value or a queue,
never shared mutable state.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import timedelta

from omni.core.clock import Clock, MonotonicTime, Pacer

__all__ = ["Repeating", "Waking"]


class Repeating:
    """Runs an action on its own daemon thread, once a beat, until stopped.

    The first run starts at once. A run that raises is reported to ``on_error`` and the beat goes on,
    so one bad pass can't silently stop the work while the board keeps drawing its last result.
    """

    def __init__(
        self,
        name: str,
        clock: Clock,
        every: timedelta,
        action: Callable[[], None],
        *,
        on_error: Callable[[Exception], None],
    ) -> None:
        self._clock = clock
        self._every = every
        self._action = action
        self._on_error = on_error
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Ask the thread to finish, and wait up to ``timeout`` seconds for its current run to end."""
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        pacer = Pacer(self._clock, self._every, sleep=self._stopped.wait)  # a stop cuts the wait short
        while not self._stopped.is_set():
            try:
                self._action()
            except Exception as exc:  # the work's failure is reported; the thread stays up
                self._on_error(exc)
            pacer.wait()


class Waking:
    """Runs an action on its own daemon thread whenever it's due, until stopped.

    The action returns when it next wants to run (a monotonic deadline, or None for no deadline of its
    own). It runs again at that deadline, at the latest after ``at_least_every``, or as soon as `wake` is
    called: when something it depends on has changed. The first run starts at once. A run that raises is
    reported to ``on_error`` and the next one comes after ``at_least_every``.
    """

    def __init__(
        self,
        name: str,
        clock: Clock,
        action: Callable[[], MonotonicTime | None],
        *,
        at_least_every: timedelta,
        on_error: Callable[[Exception], None],
    ) -> None:
        if at_least_every <= timedelta(0):
            raise ValueError("a waking worker needs a positive longest wait")
        self._clock = clock
        self._action = action
        self._at_least_every = at_least_every
        self._on_error = on_error
        self._woken = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wake(self) -> None:
        """Run again now, rather than at the next deadline."""
        self._woken.set()

    def stop(self, timeout: float | None = None) -> None:
        """Ask the thread to finish, and wait up to ``timeout`` seconds for its current run to end."""
        self._stopped.set()
        self._woken.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        while not self._stopped.is_set():
            started = self._clock.monotonic()
            self._woken.clear()  # a change from here on wakes the next wait
            latest = started + self._at_least_every
            try:
                deadline = self._action()
            except Exception as exc:  # the work's failure is reported; the thread stays up
                self._on_error(exc)
                deadline = None
            until = min(deadline, latest) if deadline is not None else latest
            wait = until.since(self._clock.monotonic()).total_seconds()
            if wait > 0:
                self._woken.wait(wait)
