"""Poller: runs the plan's due asks inline or on each source's workers, and files answers stamped on arrival."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone

from omni.core.clock import ClockReading, FakeClock, SystemClock
from omni.app.poll_plan import Ask, PollKey, PollPlan, PollPolicy, PollPriority, PollSource
from omni.app.poller import Poller

T0 = datetime(2026, 6, 17, 23, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)
_POLICY = PollPolicy(every=timedelta(seconds=15), keep_for=timedelta(seconds=90))
_SOON = 5.0


def _until(condition: Callable[[], bool], *, within: float = _SOON) -> bool:
    """Whether ``condition`` comes true within a few seconds, checked every millisecond."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.001)
    return False


class _Watched(PollPlan):
    """The plan, keeping a list of every ask it hands out and counting the dispatcher's passes."""

    def __init__(self, policies: Mapping[PollSource, PollPolicy]) -> None:
        super().__init__(policies)
        self.handed: list[Ask] = []
        self.passes = 0

    def take_due(self, at: ClockReading, *, room: Mapping[PollSource, int] | None = None) -> list[Ask]:
        asks = super().take_due(at, room=room)
        self.handed.extend(asks)
        self.passes += 1
        return asks


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
    poller = Poller(plan, clock)
    heard: list[str] = []
    poller.listen(lambda: heard.append("filed"))
    assert poller.run_due() == 2
    assert heard == ["filed", "filed"]  # a listener hears each answer and each failure as it's filed
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


def test_an_ask_that_waits_past_its_lease_to_start_is_given_up_and_never_sent() -> None:
    plan, clock = PollPlan({PollSource.GAME_FEED: _POLICY}), FakeClock(T0)
    slow_key: PollKey[str] = PollKey(PollSource.GAME_FEED, date(2026, 6, 1))
    waiting_key: PollKey[str] = PollKey(PollSource.GAME_FEED, date(2026, 6, 2))
    sent: list[str] = []

    def slow(asked: datetime) -> str:
        sent.append("slow")
        clock.advance(31 * S)  # a request that takes longer than an ask may wait to start
        return "slow"

    def waiting(asked: datetime) -> str:
        sent.append("waiting")
        return "waiting"

    plan.want(slow_key, slow, priority=PollPriority.NORMAL, at=clock.read())
    plan.want(waiting_key, waiting, priority=PollPriority.NORMAL, at=clock.read())
    assert Poller(plan, clock).run_due() == 2
    assert sent == ["slow"]  # handed out with the first, its lease ran out before its turn: no request
    assert plan.last_error(waiting_key) == "no answer within 30s"
    answer = plan.latest(slow_key, clock.read())
    assert answer is not None and answer.value == "slow"


def test_hung_game_feeds_leave_the_slate_its_own_worker_and_are_not_asked_again_while_they_hang() -> None:
    clock = FakeClock(T0)
    feed_policy = PollPolicy(every=15 * S, keep_for=90 * S, at_once=4)
    plan = _Watched({PollSource.SLATE: _POLICY, PollSource.GAME_FEED: feed_policy})
    release = threading.Event()
    calls: dict[str, int] = {}
    counting = threading.Lock()

    def hangs(name: str) -> Callable[[datetime], str]:
        def fetch(asked: datetime) -> str:
            with counting:
                calls[name] = calls.get(name, 0) + 1
                call = calls[name]
            release.wait(_SOON)
            return f"{name} #{call}"

        return fetch

    feeds: list[PollKey[str]] = [PollKey(PollSource.GAME_FEED, date(2026, 6, day)) for day in range(1, 6)]
    for day, key in enumerate(feeds, start=1):
        plan.want(key, hangs(f"feed {day}"), priority=PollPriority.NORMAL, at=clock.read())
    slate: PollKey[str] = PollKey(PollSource.SLATE, date(2026, 6, 17))
    plan.want(slate, lambda asked: "the slate", priority=PollPriority.NORMAL, at=clock.read())

    def feeds_handed() -> int:
        return sum(1 for ask in list(plan.handed) if ask.key.source is PollSource.GAME_FEED)

    poller = Poller(plan, clock, check_every=timedelta(milliseconds=1))
    poller.start()
    try:
        assert _until(lambda: sum(calls.values()) == 4)  # four feeds hang on the feed workers
        assert _until(lambda: plan.latest(slate, clock.read()) is not None)  # the slate still starts and answers
        assert feeds_handed() == 4  # the fifth feed waits for a feed worker, not in a queue
        clock.advance(31 * S)  # the hung feeds run past their leases
        assert _until(lambda: plan.last_error(feeds[0]) == "no answer within 30s")
        clock.advance(2 * S)  # their retries fall due while every feed worker is still held
        passes = plan.passes
        assert _until(lambda: plan.passes > passes + 2)
        assert feeds_handed() == 4  # so none is asked again alongside its hung request
        release.set()
        assert _until(lambda: all(plan.latest(key, clock.read()) is not None for key in feeds))
        answers = [plan.latest(key, clock.read()) for key in feeds]
        # Each hung request's late answer was ignored; each feed holds its retry's (the fifth, its first).
        assert [answer.value for answer in answers if answer is not None] == [
            "feed 1 #2",
            "feed 2 #2",
            "feed 3 #2",
            "feed 4 #2",
            "feed 5 #1",
        ]
    finally:
        release.set()
        poller.stop(_SOON)


class _Stalling(_Watched):
    """The plan, with the dispatcher stalling once, for half a minute, right after it hands an ask out."""

    def __init__(self, policies: Mapping[PollSource, PollPolicy], clock: FakeClock) -> None:
        super().__init__(policies)
        self._clock = clock
        self.stalled = False

    def take_due(self, at: ClockReading, *, room: Mapping[PollSource, int] | None = None) -> list[Ask]:
        asks = super().take_due(at, room=room)
        if asks and not self.stalled:
            self.stalled = True
            self._clock.advance(31 * S)
        return asks


def test_on_the_workers_an_ask_that_waited_past_its_lease_makes_no_request() -> None:
    clock = FakeClock(T0)
    plan = _Stalling({PollSource.GAME_FEED: _POLICY}, clock)
    key: PollKey[str] = PollKey(PollSource.GAME_FEED, date(2026, 6, 1))
    sent: list[datetime] = []

    def fetch(asked: datetime) -> str:
        sent.append(asked)
        return "answered"

    plan.want(key, fetch, priority=PollPriority.NORMAL, at=clock.read())
    poller = Poller(plan, clock, check_every=timedelta(milliseconds=1))
    poller.start()
    try:
        assert _until(lambda: plan.last_error(key) == "no answer within 30s")
        assert sent == []  # its worker reached it past its lease: no request
        clock.advance(2 * S)  # its retry falls due, and goes out at once
        assert _until(lambda: plan.latest(key, clock.read()) is not None)
        assert sent == [T0 + 33 * S]
    finally:
        poller.stop(_SOON)
