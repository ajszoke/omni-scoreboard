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

from omni.app.clock import Clock, Pacer

__all__ = ["Repeating"]


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
