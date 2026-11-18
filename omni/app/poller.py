"""Poller: asks what the `PollPlan` says is due, and files the answers back.

`run_due` asks everything due right now, one after another, on the caller's thread. Tests, replay
and start-up warm-up use it. `start` runs the same work in the background: a thread checks the plan
a few times a second and starts each due ask on a worker of its own. Each source gets as many workers
as its policy's `at_once`, and the plan hands an ask out only when one of them is free, so no ask ever
waits in line behind another source's slow request: blocked game feeds can't hold up the slate. A
request still running after the plan gave up on it keeps its worker until it returns, so a source that
hangs ties up only its own workers.

Right before a request, the worker checks with the plan that the ask is still wanted (`PollPlan.begin`),
so an ask given up on, or for a key dropped meanwhile, never reaches the network. Each answer is
stamped with the clock's two readings as it arrives; any exception from a fetch is filed as that ask's
failure, and either way with what the ask was billed, as its fetchers' meters tallied it
(`omni.app.quota.tallying`). Whoever reads the answers can `listen`, to hear when one is filed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from omni.app.background import Repeating
from omni.app.quota import tallying
from omni.core.clock import Clock
from omni.app.poll_plan import Ask, PollPlan, PollSource

__all__ = ["Poller"]

_CHECK_EVERY = timedelta(milliseconds=250)


class Poller:
    """Runs the plan's due asks, inline or on each source's own workers."""

    def __init__(self, plan: PollPlan, clock: Clock, *, check_every: timedelta = _CHECK_EVERY) -> None:
        self._plan = plan
        self._clock = clock
        self._check_every = check_every
        self._capacity = plan.capacity()
        self._running: dict[PollSource, int] = {}  # asks each source has on a worker, including given-up ones
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._dispatcher: Repeating | None = None
        self._listeners: list[Callable[[], None]] = []

    def listen(self, listener: Callable[[], None]) -> None:
        """Call ``listener`` after each answer or failure is filed."""
        self._listeners.append(listener)

    def run_due(self) -> int:
        """Ask everything due now, here, in turn; returns how many asks were handed out."""
        asks = self._plan.take_due(self._clock.read())
        for ask in asks:
            self._ask(ask)
        return len(asks)

    def start(self) -> None:
        # One thread for every ask the sources may run at once, so a handed-out ask always starts at once.
        workers = max(1, sum(self._capacity.values()))
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="poll")
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
        with self._lock:
            room = {source: workers - self._running.get(source, 0) for source, workers in self._capacity.items()}
        for ask in self._plan.take_due(self._clock.read(), room=room):
            with self._lock:
                self._running[ask.key.source] = self._running.get(ask.key.source, 0) + 1
            pool.submit(self._work, ask)

    def _work(self, ask: Ask) -> None:
        """One worker's ask; its source's worker is free again once the request returns."""
        try:
            self._ask(ask)
        finally:
            with self._lock:
                self._running[ask.key.source] -= 1

    def _ask(self, ask: Ask) -> None:
        asked = self._clock.read()
        if not self._plan.begin(ask, asked):
            return  # given up on, or no longer wanted, before it started: no request
        with tallying() as billed:
            try:
                value = ask.fetch(asked.utc)
            except Exception as exc:  # any failure is this ask's, filed for the decision pass to see
                error = f"{type(exc).__name__}: {exc}"
                self._plan.failed(ask.key, error, self._clock.read(), ask=ask.number, rows=billed.rows)
            else:
                self._plan.answered(ask.key, value, self._clock.read(), ask=ask.number, rows=billed.rows)
        for listener in self._listeners:
            listener()
