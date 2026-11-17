"""PollPlan: one ask per key in flight, priority within quota, bounded retries, last good until it expires."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from omni.core.clock import FakeClock, MonotonicTime
from omni.app.poll_plan import Failure, PollKey, PollPlan, PollPolicy, PollPriority, PollSource
from omni.core.enum import League
from omni.core.ids import SourceRef
from omni.providers.base import LocalDate, PollScope

T0 = datetime(2026, 6, 17, 23, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)
FEED = PollSource.GAME_FEED
_POLICY = PollPolicy(every=15 * S, focus_every=8 * S, background_every=30 * S, keep_for=90 * S)


def _plan(policy: PollPolicy = _POLICY, *, forget_after: timedelta = 120 * S) -> tuple[PollPlan, FakeClock]:
    return PollPlan({FEED: policy}, forget_after=forget_after), FakeClock(T0)


def _scope(day: int) -> PollScope:
    return PollScope(SourceRef("test"), League.MLB, LocalDate(date(2026, 6, day)))


def _key(day: int) -> PollKey[str]:
    return PollKey(FEED, _scope(day))


def _want(plan: PollPlan, clock: FakeClock, key: PollKey[str], priority: PollPriority = PollPriority.NORMAL) -> None:
    plan.want(key, lambda asked: f"answer at {asked:%H:%M:%S}", priority=priority, at=clock.read())


def _keys(plan: PollPlan, clock: FakeClock) -> list[PollKey[object]]:
    return [ask.key for ask in plan.take_due(clock.read())]


def test_a_new_key_is_due_at_once_and_then_once_a_cadence() -> None:
    plan, clock = _plan()
    key = _key(1)
    _want(plan, clock, key)
    [ask] = plan.take_due(clock.read())
    assert ask.key == key and ask.fetch(T0) == "answer at 23:00:00"
    assert plan.take_due(clock.read()) == []  # in flight: never asked twice at once
    plan.answered(key, "first", clock.read())
    clock.advance(14 * S)
    assert _keys(plan, clock) == []
    clock.advance(S)
    assert _keys(plan, clock) == [key]  # 15 s after the answer


@pytest.mark.parametrize(
    ("priority", "cadence"), [(PollPriority.FOCUS, 8), (PollPriority.NORMAL, 15), (PollPriority.BACKGROUND, 30)]
)
def test_the_cadence_follows_priority(priority: PollPriority, cadence: int) -> None:
    plan, clock = _plan()
    key = _key(1)
    _want(plan, clock, key, priority)
    plan.take_due(clock.read())
    plan.answered(key, "a", clock.read())
    clock.advance((cadence - 1) * S)
    assert _keys(plan, clock) == []
    clock.advance(S)
    assert _keys(plan, clock) == [key]


def test_a_policy_without_focus_or_background_cadences_uses_its_own() -> None:
    plain = PollPolicy(every=15 * S, keep_for=60 * S)
    assert {plain.cadence(priority) for priority in PollPriority} == {15 * S}


def test_a_promoted_key_comes_due_by_its_faster_cadence() -> None:
    plan, clock = _plan()
    key = _key(1)
    _want(plan, clock, key)
    plan.take_due(clock.read())
    plan.answered(key, "a", clock.read())
    clock.advance(8 * S)
    _want(plan, clock, key, PollPriority.FOCUS)  # the game turned into a favorite's close one
    assert _keys(plan, clock) == [key]


def test_the_quota_goes_to_the_most_important_first_and_the_rest_wait() -> None:
    plan, clock = _plan(PollPolicy(every=15 * S, keep_for=90 * S, per_minute=2))
    background, normal, focus = _key(1), _key(2), _key(3)
    _want(plan, clock, background, PollPriority.BACKGROUND)
    _want(plan, clock, normal)
    _want(plan, clock, focus, PollPriority.FOCUS)
    assert _keys(plan, clock) == [focus, normal]
    clock.advance(29 * S)
    assert _keys(plan, clock) == []  # not a whole token back yet
    clock.advance(S)
    assert _keys(plan, clock) == [background]


def test_a_failing_key_is_retried_a_few_times_then_waits_its_cadence() -> None:
    plan, clock = _plan()
    key = _key(1)
    _want(plan, clock, key)
    for retry in (2, 5, 15, 15):  # three quick retries, then its normal cadence
        plan.take_due(clock.read())
        plan.failed(key, "TimeoutError: slow", clock.read())
        assert plan.last_error(key) == "TimeoutError: slow"
        clock.advance((retry - 1) * S)
        assert _keys(plan, clock) == []
        clock.advance(S)
        _want(plan, clock, key)
    plan.take_due(clock.read())
    plan.answered(key, "back", clock.read())
    assert plan.last_error(key) is None


def test_a_failing_key_says_why_and_when_it_failed_until_it_answers() -> None:
    plan, clock = _plan()
    key = _key(1)
    assert plan.failure(key) is None  # never asked about
    _want(plan, clock, key)
    plan.take_due(clock.read())
    clock.advance(3 * S)
    failed = clock.read()
    plan.failed(key, "TimeoutError: slow", failed)
    clock.advance(S)
    assert plan.failure(key) == Failure("TimeoutError: slow", failed)  # filed when it failed, not when read
    clock.advance(S)
    plan.take_due(clock.read())
    plan.answered(key, "back", clock.read())
    assert plan.failure(key) is None


def test_the_last_good_answer_stands_through_failures_until_it_expires() -> None:
    plan, clock = _plan()
    key = _key(1)
    _want(plan, clock, key)
    plan.take_due(clock.read())
    plan.answered(key, "good", clock.read())
    clock.advance(20 * S)
    plan.take_due(clock.read())
    plan.failed(key, "ConnectionError: down", clock.read())
    standing = plan.latest(key, clock.read())
    assert standing is not None and standing.value == "good" and standing.received.utc == T0
    clock.advance(70 * S)
    assert plan.latest(key, clock.read()) is not None  # 90 s old: still stands
    clock.advance(S)
    assert plan.latest(key, clock.read()) is None


def test_an_answer_can_be_read_as_of_an_earlier_receipt() -> None:
    plan, clock = _plan()
    key = _key(1)
    _want(plan, clock, key)
    for value in ("first", "second", "third"):
        plan.take_due(clock.read())
        plan.answered(key, value, clock.read())
        clock.advance(15 * S)
    before_second = MonotonicTime(20)  # 20 s after the first, on the monotonic clock
    answer = plan.latest_by(key, before_second, clock.read())
    assert answer is not None and answer.value == "second"
    assert plan.latest_by(key, MonotonicTime(-1), clock.read()) is None  # nothing that old
    clock.advance(80 * S)
    assert plan.latest_by(key, before_second, clock.read()) is None  # it has expired since


def test_a_key_nobody_wants_is_forgotten_even_in_flight() -> None:
    plan, clock = _plan(forget_after=60 * S)
    kept, dropped = _key(1), _key(2)
    _want(plan, clock, kept)
    _want(plan, clock, dropped)
    plan.take_due(clock.read())
    clock.advance(50 * S)
    _want(plan, clock, kept)
    clock.advance(11 * S)
    plan.take_due(clock.read())  # forgets what no decision has asked about for a minute
    assert len(plan) == 1
    plan.answered(dropped, "late", clock.read())  # its answer lands after it was forgotten
    plan.failed(dropped, "late failure", clock.read())
    assert plan.latest(dropped, clock.read()) is None and plan.last_error(dropped) is None
    assert plan.latest_by(dropped, MonotonicTime(60), clock.read()) is None


def test_the_next_deadline_is_the_earliest_idle_due_time() -> None:
    plan, clock = _plan()
    assert plan.next_due() is None
    first, second = _key(1), _key(2)
    _want(plan, clock, first)
    plan.take_due(clock.read())
    assert plan.next_due() == clock.monotonic() + 30 * S  # the only key is in flight: when it's given up on
    plan.answered(first, "a", clock.read())
    clock.advance(3 * S)
    _want(plan, clock, second, PollPriority.BACKGROUND)
    assert plan.next_due() == clock.monotonic()  # the new key, due now
    plan.take_due(clock.read())
    due = plan.next_due()
    assert due is not None and due.seconds == 15.0


def test_an_ask_with_no_answer_in_time_is_given_up_on_and_its_late_answer_ignored() -> None:
    plan, clock = _plan()
    key = _key(1)
    _want(plan, clock, key)
    [hung] = plan.take_due(clock.read())
    clock.advance(29 * S)
    assert plan.take_due(clock.read()) == []  # still in flight
    clock.advance(1 * S)
    assert plan.take_due(clock.read()) == []  # given up on: retried on the short schedule
    assert plan.last_error(key) == "no answer within 30s"
    clock.advance(2 * S)
    [retry] = plan.take_due(clock.read())
    assert retry.number > hung.number
    plan.answered(key, "late", clock.read(), ask=hung.number)  # the hung request finally returns
    plan.failed(key, "late failure", clock.read(), ask=hung.number)
    assert plan.latest(key, clock.read()) is None and plan.last_error(key) == "no answer within 30s"
    plan.answered(key, "fresh", clock.read(), ask=retry.number)
    answer = plan.latest(key, clock.read())
    assert answer is not None and answer.value == "fresh" and plan.last_error(key) is None


def test_a_poll_policy_needs_positive_spans_and_a_usable_quota() -> None:
    with pytest.raises(ValueError, match="positive"):
        PollPolicy(every=S, keep_for=S, answer_within=timedelta(0))
    with pytest.raises(ValueError, match="positive"):
        PollPolicy(every=timedelta(0), keep_for=S)
    with pytest.raises(ValueError, match="positive"):
        PollPolicy(every=S, keep_for=S, retries=(timedelta(0),))
    with pytest.raises(ValueError, match="at least one ask"):
        PollPolicy(every=S, keep_for=S, per_minute=0)
    with pytest.raises(ValueError, match="at least one worker"):
        PollPolicy(every=S, keep_for=S, at_once=0)


def test_ask_numbers_are_never_reused_when_a_key_is_dropped_and_wanted_again() -> None:
    plan, clock = _plan(forget_after=60 * S)
    key = _key(1)
    _want(plan, clock, key)
    [dropped] = plan.take_due(clock.read())
    clock.advance(61 * S)
    plan.take_due(clock.read())  # nobody wanted it for a minute: forgotten, its ask still out
    assert not plan.begin(dropped, clock.read())  # a worker reaching it now makes no request
    _want(plan, clock, key)  # wanted again
    [fresh] = plan.take_due(clock.read())
    assert fresh.number != dropped.number
    plan.answered(key, "late", clock.read(), ask=dropped.number)  # the dropped ask's answer lands
    assert plan.latest(key, clock.read()) is None and not plan.begin(dropped, clock.read())
    assert plan.begin(fresh, clock.read())
    plan.answered(key, "fresh", clock.read(), ask=fresh.number)
    answer = plan.latest(key, clock.read())
    assert answer is not None and answer.value == "fresh"


def test_a_due_ask_waits_for_a_free_worker_of_its_own_source() -> None:
    slate: PollKey[str] = PollKey(PollSource.SLATE, _scope(17))
    plan = PollPlan({FEED: PollPolicy(every=15 * S, keep_for=90 * S, at_once=2), PollSource.SLATE: _POLICY})
    clock = FakeClock(T0)
    assert plan.capacity() == {FEED: 2, PollSource.SLATE: 1}
    feeds = [_key(day) for day in (1, 2, 3)]
    for key in feeds:
        _want(plan, clock, key)
    _want(plan, clock, slate)
    room = {FEED: 1, PollSource.SLATE: 1}
    assert [ask.key for ask in plan.take_due(clock.read(), room=room)] == [feeds[0], slate]
    assert plan.take_due(clock.read(), room={FEED: 0}) == []  # no worker free: the rest stay due
    assert [ask.key for ask in plan.take_due(clock.read(), room={FEED: 2})] == feeds[1:]


def test_a_started_ask_has_its_whole_lease_from_when_it_started() -> None:
    plan, clock = _plan()
    key = _key(1)
    _want(plan, clock, key)
    [ask] = plan.take_due(clock.read())
    clock.advance(20 * S)
    assert plan.begin(ask, clock.read())  # a worker starts it 20 s after it was handed out
    clock.advance(29 * S)
    assert plan.take_due(clock.read()) == [] and plan.last_error(key) is None  # 29 s into its request
    clock.advance(S)
    plan.take_due(clock.read())
    assert plan.last_error(key) == "no answer within 30s"
    assert not plan.begin(ask, clock.read())  # given up on: a worker would make no request


def test_an_ask_not_started_within_its_lease_is_given_up_when_a_worker_reaches_it() -> None:
    plan, clock = _plan()
    key = _key(1)
    _want(plan, clock, key)
    [ask] = plan.take_due(clock.read())
    clock.advance(30 * S)
    assert not plan.begin(ask, clock.read())
    assert plan.last_error(key) == "no answer within 30s"
    clock.advance(2 * S)
    assert _keys(plan, clock) == [key]  # retried on the short schedule


def test_answers_are_kept_while_they_stand_however_many_arrive() -> None:
    plan, clock = _plan()
    key = _key(1)
    _want(plan, clock, key)
    for value in range(9):  # an answer every 10 s: 80 s of them, all still standing
        plan.take_due(clock.read())
        plan.answered(key, f"answer {value}", clock.read())
        clock.advance(10 * S)
        _want(plan, clock, key)
    oldest = plan.latest_by(key, MonotonicTime(0), clock.read())
    assert oldest is not None and oldest.value == "answer 0"
    clock.advance(11 * S)  # the first is 101 s old: it no longer stands
    plan.answered(key, "newest", clock.read())
    assert plan.latest_by(key, MonotonicTime(0), clock.read()) is None
