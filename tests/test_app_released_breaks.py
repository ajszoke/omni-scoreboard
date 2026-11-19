"""Released breaks and action: each break carries its answer's pinned release, and released action names the
breaks it closed, only those it came after (`omni.app.policy_facts.ReleasedBreaks`)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from omni.app.policy_facts import ReleasedBreaks
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind, OnsetBasis
from omni.events.break_detection import ActionEvidence
from omni.queue.delay_policy import Arrival, DelayPolicy, ReleaseGuard

T0 = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)
GAME = LeagueScopedId(League.NFL, SourceRef("nflmeta"), "401")


def _at(seconds: float) -> ClockReading:
    return ClockReading(T0 + seconds * S, MonotonicTime(float(seconds)))


def _break(kind: BreakKind, marker: str, *, received: float, onset: float | None, basis: OnsetBasis) -> BreakEvidence:
    reading = _at(received)
    return BreakEvidence(
        id=BreakId(GAME, kind, marker),
        received=reading,
        confidence=BreakConfidence.EXPLICIT,
        onset=None if onset is None else T0 + onset * S,
        as_of=reading.utc,
        onset_basis=basis,
    )


def _snap(play: int, *, received: float, written: float | None) -> ActionEvidence:
    return ActionEvidence(
        contest=GAME, marker=f"play {play}", received=_at(received), at=None if written is None else T0 + written * S
    )


class _Track:
    def __init__(self) -> None:
        self.breaks = ReleasedBreaks(ReleaseGuard(DelayPolicy(broadcast_lag=DurationSeconds(45))))

    def answer(self, received: float, *breaks: BreakEvidence, action: ActionEvidence | None = None) -> None:
        reading = _at(received)
        self.breaks.push(breaks, action, Arrival(received=reading, as_of=reading.utc), reading)


def test_a_released_break_carries_the_release_pinned_for_its_answer_however_late_it_is_read() -> None:
    track = _Track()
    timeout = _break(BreakKind.NFL_TEAM_TIMEOUT, "play 12", received=0, onset=-3, basis=OnsetBasis.PUBLISHED)
    track.answer(0, timeout)
    track.breaks.release(_at(60))  # read 15 s after it cleared the delay
    assert track.breaks.breaks == (replace(timeout, released=MonotonicTime(45.0)),)


def test_released_action_closes_only_the_breaks_it_came_after_and_names_each_it_has_closed() -> None:
    """A late row of a snap written before the timeout is newer action, but closes nothing; the next snap, written
    after the timeout, closes it, and the action that follows still names it."""
    track = _Track()
    timeout = _break(BreakKind.NFL_TEAM_TIMEOUT, "play 12", received=0, onset=-3, basis=OnsetBasis.PUBLISHED)
    track.answer(0, timeout, action=_snap(10, received=0, written=-20))
    track.answer(10, action=_snap(11, received=10, written=-8))  # written before the timeout's row, read late
    track.answer(20, action=_snap(13, received=20, written=15))
    track.answer(30, action=_snap(14, received=30, written=25))
    track.breaks.release(_at(55))
    assert [evidence.id for evidence in track.breaks.breaks] == [timeout.id]
    action = track.breaks.action
    assert action is not None and action.marker == "play 11" and action.closes == frozenset()
    track.breaks.release(_at(65))
    action = track.breaks.action
    assert track.breaks.breaks == () and action is not None and action.closes == {timeout.id}
    track.breaks.release(_at(75))
    assert track.breaks.action is not None and track.breaks.action.marker == "play 14"
    assert track.breaks.action.closes == {timeout.id}


def test_a_local_hold_is_closed_by_newer_action_whatever_the_feed_s_clock_says() -> None:
    """Halftime's hold runs on this board's clock, so a snap's time on the feed's clock can't be compared with its
    start: the first newer snap closes it."""
    track = _Track()
    halftime = _break(BreakKind.NFL_HALFTIME, "halftime", received=0, onset=-60, basis=OnsetBasis.LOCAL_HOLD)
    track.answer(0, halftime, action=_snap(200, received=0, written=-900))
    track.answer(10, action=_snap(201, received=10, written=-600))  # the feed's clock far behind ours
    track.breaks.release(_at(55))
    assert track.breaks.breaks == () and track.breaks.action is not None
    assert track.breaks.action.closes == {halftime.id}
