"""Poller: runs the plan's due asks inline or on workers, and files answers stamped on arrival."""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone

from omni.core.clock import FakeClock, SystemClock
from omni.app.poll_plan import PollKey, PollPlan, PollPolicy, PollPriority, PollSource
from omni.app.poller import Poller

T0 = datetime(2026, 6, 17, 23, 0, tzinfo=timezone.utc)
_POLICY = PollPolicy(every=timedelta(seconds=15), keep_for=timedelta(seconds=90))
_SOON = 5.0


def test_running_due_asks_inline_files_answers_and_failures() -> None:
    plan, clock = PollPlan({PollSource.GAME_FEED: _POLICY}), FakeClock(T0)
    good: PollKey[str] = PollKey(PollSource.GAME_FEED, date(2026, 6, 1))
    bad: PollKey[str] = PollKey(PollSource.GAME_FEED, date(2026, 6, 2))

    def slow(asked: datetime) -> str:
        clock.advance(timedelta(milliseconds=300))  # the request takes a while
        return f"asked {asked:%H:%M:%S}"

    def broken(asked: datetime) -> str:
        raise ConnectionError("refused")

    plan.want(good, slow, priority=PollPriority.NORMAL, at=clock.read())
    plan.want(bad, broken, priority=PollPriority.NORMAL, at=clock.read())
    assert Poller(plan, clock).run_due() == 2
    answer = plan.latest(good, clock.read())
    assert answer is not None and answer.value == "asked 23:00:00"
    assert answer.received.utc == T0 + timedelta(milliseconds=300)  # stamped when it arrived
    assert plan.last_error(bad) == "ConnectionError: refused"


def test_in_the_background_workers_answer_what_falls_due() -> None:
    plan = PollPlan({PollSource.GAME_FEED: _POLICY})
    clock = SystemClock()
    key: PollKey[str] = PollKey(PollSource.GAME_FEED, date(2026, 6, 1))
    asked = threading.Event()

    def fetch(at: datetime) -> str:
        asked.set()
        return "answered"

    plan.want(key, fetch, priority=PollPriority.FOCUS, at=clock.read())
    poller = Poller(plan, clock, check_every=timedelta(milliseconds=1))
    poller.start()
    assert asked.wait(_SOON)
    deadline = time.monotonic() + _SOON
    while plan.latest(key, clock.read()) is None and time.monotonic() < deadline:
        time.sleep(0.001)
    poller.stop(_SOON)
    answer = plan.latest(key, clock.read())
    assert answer is not None and answer.value == "answered"


def test_stopping_a_poller_that_never_started_is_harmless() -> None:
    Poller(PollPlan({}), SystemClock()).stop()
