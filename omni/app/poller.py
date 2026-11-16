"""Poller: asks what the `PollPlan` says is due, and files the answers back.

`run_due` asks everything due right now, one after another, on the caller's thread. Tests, replay
and start-up warm-up use it. `start` runs the same work in the background: a thread checks the plan
a few times a second and hands each due ask to a small pool of workers, so a slow source holds up
only its own ask. Each answer is stamped with the clock's two readings as it arrives; any exception
from a fetch is filed as that ask's failure.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from omni.app.background import Repeating
from omni.app.clock import Clock
from omni.app.poll_plan import Ask, PollPlan

__all__ = ["Poller"]

_CHECK_EVERY = timedelta(milliseconds=250)


class Poller:
    """Runs the plan's due asks, inline or on worker threads."""

    def __init__(
        self, plan: PollPlan, clock: Clock, *, workers: int = 4, check_every: timedelta = _CHECK_EVERY
    ) -> None:
        self._plan = plan
        self._clock = clock
        self._workers = workers
        self._check_every = check_every
        self._pool: ThreadPoolExecutor | None = None
        self._dispatcher: Repeating | None = None

    def run_due(self) -> int:
        """Ask everything due now, here, in turn; returns how many asks ran."""
        asks = self._plan.take_due(self._clock.read())
        for ask in asks:
            self._ask(ask)
        return len(asks)

    def start(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="poll")
        self._dispatcher = Repeating("poller", self._clock, self._check_every, self._dispatch, on_error=lambda _: None)
        self._dispatcher.start()

    def stop(self, timeout: float | None = None) -> None:
        if self._dispatcher is not None:
            self._dispatcher.stop(timeout)
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _dispatch(self) -> None:
        pool = self._pool
        assert pool is not None
        for ask in self._plan.take_due(self._clock.read()):
            pool.submit(self._ask, ask)

    def _ask(self, ask: Ask) -> None:
        asked = self._clock.read()
        try:
            value = ask.fetch(asked.utc)
        except Exception as exc:  # any failure is this ask's, filed for the decision pass to see
            self._plan.failed(ask.key, f"{type(exc).__name__}: {exc}", self._clock.read())
            return
        self._plan.answered(ask.key, value, self._clock.read())
