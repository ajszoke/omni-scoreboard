"""The tower's lane: segments on air one at a time, frame by frame, dwell counted from each commit.

The first block is the football play strip's own behavior, which moved onto the lane unchanged; the
rest is the presentation contract the lane adds.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from omni.app.clock import ClockReading, MonotonicTime
from omni.core.ids import SourceRef
from omni.domain.football import PlaySummary, PlayTone
from omni.tower import (
    ChangeKind,
    Cue,
    Dwell,
    EndReason,
    Hold,
    Lane,
    Provenance,
    Segment,
    SegmentKey,
    Surface,
    earliest,
)

T0 = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)
MIN, MAX = 10 * S, 20 * S
DWELL = Dwell(minimum=MIN, maximum=MAX)
SOURCE = SourceRef("test")


def _at(moment: datetime) -> ClockReading:
    return ClockReading(utc=moment, monotonic=MonotonicTime((moment - T0).total_seconds()))


def _segment(
    key: str,
    headline: str = "RUN +4",
    players: str | None = "C.Brown 4 YDS 1 CAR",
    *,
    revision: int = 0,
    hold: Hold | None = None,
    available: datetime = T0,
    latest_start: datetime | None = None,
    expires_at: datetime | None = None,
    surface: Surface = Surface.PLAY_STRIP,
) -> Segment[PlaySummary]:
    description = PlaySummary(tone=PlayTone.STANDARD, headline=headline, detail=f"{headline} detail")
    frames = (description, replace(description, detail=players)) if players else (description,)
    return Segment(
        key=SegmentKey(surface, key),
        frames=frames,
        dwell=DWELL,
        provenance=Provenance(SOURCE, received_at=T0),
        available_at=available,
        revision=revision,
        hold=hold,
        latest_start=latest_start,
        expires_at=expires_at,
    )


def _stoppage(key: str, headline: str) -> Segment[PlaySummary]:
    return replace(
        _segment(key, headline, players=None, hold=Hold.PLAY_RESUMES),
        frames=(PlaySummary(tone=PlayTone.STANDARD, headline=headline),),
    )


def _air(lane: Lane[PlaySummary], moment: datetime) -> tuple[str, str | None] | None:
    """What a display that draws every cue at once shows at ``moment``."""
    at = _at(moment)
    cue = lane.cue(at)
    if cue is None:
        return None
    lane.committed(cue, at)
    return cue.payload.headline, cue.payload.detail


def _lane(*segments: Segment[PlaySummary]) -> Lane[PlaySummary]:
    lane: Lane[PlaySummary] = Lane(Surface.PLAY_STRIP)
    lane.offer(segments)
    return lane


# The play strip's behavior, as it was before the lane.


def test_a_lone_segment_shows_its_description_then_its_players_then_nothing() -> None:
    lane = _lane()
    assert _air(lane, T0) is None
    lane.offer([_segment("1")])
    assert _air(lane, T0) == ("RUN +4", "RUN +4 detail")
    assert _air(lane, T0 + MAX - S) == ("RUN +4", "RUN +4 detail")
    assert _air(lane, T0 + MAX) == ("RUN +4", "C.Brown 4 YDS 1 CAR")
    assert _air(lane, T0 + 2 * MAX) is None


def test_a_segment_of_one_frame_goes_blank_after_it() -> None:
    lane = _lane(_segment("1", "TV TIMEOUT", players=None))
    _air(lane, T0)
    assert _air(lane, T0 + MAX) is None


def test_segments_offered_together_each_get_their_turn() -> None:
    lane = _lane(_segment("1", "RUN +11"), _segment("2", "PASS +53"), _segment("1", "RUN +11"))
    assert _air(lane, T0) == ("RUN +11", "RUN +11 detail")
    assert _air(lane, T0 + MIN - S) == ("RUN +11", "RUN +11 detail")
    later = T0 + MIN
    assert _air(lane, later) == ("PASS +53", "PASS +53 detail")  # one waiting: the minimum, then on
    assert _air(lane, later + MAX - S) == ("PASS +53", "PASS +53 detail")  # the last gets it all
    assert _air(lane, later + MAX) == ("PASS +53", "C.Brown 4 YDS 1 CAR")
    lane.offer([_segment("1", "RUN +11")])  # already shown once
    assert _air(lane, later + 2 * MAX) is None


def test_an_invalidated_segment_goes_once_it_has_had_its_minimum() -> None:
    lane = _lane(_segment("1"))
    _air(lane, T0)
    lane.invalidate()
    assert _air(lane, T0 + MIN - S) == ("RUN +4", "RUN +4 detail")
    assert _air(lane, T0 + MIN) is None
    lane.offer([_segment("2", "PASS +9")])
    assert _air(lane, T0 + MIN + S) == ("PASS +9", "PASS +9 detail")  # a fresh one isn't stale
    assert _air(lane, T0 + MIN + S + MAX) == ("PASS +9", "C.Brown 4 YDS 1 CAR")


def test_closing_out_gives_each_segment_its_minimum_and_no_later_frames() -> None:
    lane = _lane(_segment("1", "KNEEL"), _segment("2", "TV TIMEOUT"))
    _air(lane, T0)
    lane.close_out()
    assert _air(lane, T0 + MIN - S) == ("KNEEL", "KNEEL detail")
    assert _air(lane, T0 + MIN) == ("TV TIMEOUT", "TV TIMEOUT detail")
    assert not lane.idle
    assert _air(lane, T0 + 2 * MIN) is None and lane.idle
    lane.resume()
    lane.offer([_segment("3")])
    _air(lane, T0 + 3 * MIN)
    assert _air(lane, T0 + 3 * MIN + MAX) == ("RUN +4", "C.Brown 4 YDS 1 CAR")
    assert lane.clear(_at(T0 + 3 * MIN + MAX)) == []
    assert _air(lane, T0 + 6 * MIN) is None


def test_a_held_segment_stays_up_until_play_resumes() -> None:
    lane = _lane(_stoppage("t", "TV TIMEOUT"))
    _air(lane, T0)
    assert _air(lane, T0 + timedelta(minutes=3)) == ("TV TIMEOUT", None)  # however long the break runs
    lane.invalidate()  # the next snap
    assert _air(lane, T0 + timedelta(minutes=3)) is None
    lane.offer([_stoppage("i", "INJURY CIN"), _segment("2")])
    _air(lane, T0 + timedelta(minutes=4))
    assert _air(lane, T0 + timedelta(minutes=4) + MIN) == ("RUN +4", "RUN +4 detail")  # superseded


def test_each_frame_gets_its_own_minimum() -> None:
    lane = _lane(_segment("1"))
    _air(lane, T0)
    assert _air(lane, T0 + MAX) == ("RUN +4", "C.Brown 4 YDS 1 CAR")  # the players' frame starts
    lane.invalidate()  # a second later the field moves on
    assert _air(lane, T0 + MAX + MIN - S) == ("RUN +4", "C.Brown 4 YDS 1 CAR")  # its own 10s
    assert _air(lane, T0 + MAX + MIN) is None  # then blank, waiting for the play


def test_a_segment_waiting_when_a_description_has_had_its_minimum_skips_the_later_frames() -> None:
    lane = _lane(_segment("1"))
    _air(lane, T0)
    lane.offer([_segment("2", "PASS +9")])
    assert _air(lane, T0 + MIN) == ("PASS +9", "PASS +9 detail")  # straight up, no blank


# The presentation contract.


def test_a_frame_that_never_drew_keeps_its_whole_dwell() -> None:
    lane = _lane(_segment("1"), _segment("2", "PASS +9"))
    cue = lane.cue(_at(T0))
    assert cue is not None and lane.cue(_at(T0 + timedelta(minutes=5))) == cue  # undrawn: nothing runs
    lane.committed(cue, _at(T0 + timedelta(minutes=5)))
    later = T0 + timedelta(minutes=5)
    assert _air(lane, later + MIN - S) == ("RUN +4", "RUN +4 detail")  # its minimum from the commit
    assert _air(lane, later + MIN) == ("PASS +9", "PASS +9 detail")


def test_a_later_frame_counts_from_its_own_commit() -> None:
    lane = _lane(_segment("1"))
    _air(lane, T0)
    cue = lane.cue(_at(T0 + MAX))
    assert cue is not None and cue.frame == 1
    lane.committed(cue, _at(T0 + MAX + 7 * S))  # the players' frame reached the display late
    assert _air(lane, T0 + MAX + 7 * S + MAX - S) == ("RUN +4", "C.Brown 4 YDS 1 CAR")
    assert _air(lane, T0 + MAX + 7 * S + MAX) is None


def test_a_commit_of_what_is_no_longer_on_air_or_already_running_changes_nothing() -> None:
    lane = _lane(_segment("1"), _segment("2"))
    first = lane.cue(_at(T0))
    assert first is not None
    lane.committed(first, _at(T0))
    lane.committed(first, _at(T0 + 5 * S))  # drawn again: its dwell already runs from T0
    assert lane.cue(_at(T0 + MIN)) != first
    lane.committed(first, _at(T0 + MIN))  # a stale cue: the second is on air now, uncommitted
    assert lane.cue(_at(T0 + 2 * MIN + MAX)) is not None  # so the second is still waiting for its commit
    assert [change.kind for change in lane.changes()] == [ChangeKind.UP, ChangeKind.DOWN]


def test_changes_report_each_segment_up_framed_down_and_dropped_with_why() -> None:
    lane = _lane(_segment("1"), _segment("2", players=None))
    _air(lane, T0)
    _air(lane, T0 + MIN)  # the second was waiting
    lane.invalidate()
    _air(lane, T0 + 2 * MIN)
    lane.offer([_segment("3")])
    _air(lane, T0 + 3 * MIN)
    _air(lane, T0 + 3 * MIN + MAX)  # its players' frame
    _air(lane, T0 + 3 * MIN + 2 * MAX)  # it ran its time
    lane.offer([_segment("4"), _segment("5", players=None)])
    _air(lane, T0 + 8 * MIN)
    lane.close_out()
    _air(lane, T0 + 9 * MIN)  # closing: the fourth gets its minimum, then the fifth goes up
    lane.resume()
    assert lane.clear(_at(T0 + 10 * MIN)) == []
    changes = lane.changes()
    assert [(change.kind, change.segment.key.subject, change.frame, change.reason) for change in changes] == [
        (ChangeKind.UP, "1", 0, None),
        (ChangeKind.DOWN, "1", 0, EndReason.NEXT_WAITING),
        (ChangeKind.UP, "2", 0, None),
        (ChangeKind.DOWN, "2", 0, EndReason.INVALIDATED),
        (ChangeKind.UP, "3", 0, None),
        (ChangeKind.FRAME, "3", 1, None),
        (ChangeKind.DOWN, "3", 1, EndReason.RAN_ITS_TIME),
        (ChangeKind.UP, "4", 0, None),
        (ChangeKind.DOWN, "4", 0, EndReason.CLOSING),
        (ChangeKind.UP, "5", 0, None),
        (ChangeKind.DOWN, "5", 0, EndReason.CLEARED),
    ]
    assert changes[0].at == _at(T0) and lane.changes() == []


def test_clearing_drops_what_waits_and_what_never_drew() -> None:
    lane = _lane(_segment("1"), _segment("2"))
    lane.cue(_at(T0))  # cued, never drawn
    assert [segment.key.subject for segment in lane.clear(_at(T0 + S))] == ["1", "2"]
    assert lane.idle and lane.changes() == []


def test_a_segment_waits_until_it_is_available_and_only_then_presses_on_what_is_up() -> None:
    lane = _lane(_segment("1"), _segment("2", "PASS +9", available=T0 + 15 * S))
    _air(lane, T0)
    assert _air(lane, T0 + 14 * S) == ("RUN +4", "RUN +4 detail")  # waiting, but not yet available
    assert _air(lane, T0 + 15 * S) == ("PASS +9", "PASS +9 detail")
    lane = _lane(_segment("3", available=T0 + 30 * S))
    assert _air(lane, T0) is None and _air(lane, T0 + 30 * S) == ("RUN +4", "RUN +4 detail")


def test_a_segment_past_its_latest_start_or_expiry_is_dropped_unseen() -> None:
    lane = _lane(
        _segment("1"),
        _segment("2", latest_start=T0 + 5 * S),
        _segment("3", expires_at=T0 + 8 * S),
        _segment("4", "PASS +9"),
    )
    _air(lane, T0)
    assert _air(lane, T0 + MIN) == ("PASS +9", "PASS +9 detail")
    assert [(c.kind, c.segment.key.subject, c.reason) for c in lane.changes()] == [
        (ChangeKind.UP, "1", None),
        (ChangeKind.DOWN, "1", EndReason.NEXT_WAITING),
        (ChangeKind.DROPPED, "2", EndReason.TOO_LATE),
        (ChangeKind.DROPPED, "3", EndReason.EXPIRED),
        (ChangeKind.UP, "4", None),
    ]


def test_an_expiry_takes_a_segment_down_at_once_on_air_or_not() -> None:
    lane = _lane(_segment("1", expires_at=T0 + 3 * S))
    _air(lane, T0)
    assert _air(lane, T0 + 3 * S) is None  # before its minimum: an expiry isn't something waiting
    lane.offer([_segment("2", expires_at=T0 + 9 * S)])
    assert lane.cue(_at(T0 + 4 * S)) is not None  # cued, never drawn
    assert lane.cue(_at(T0 + 9 * S)) is None
    assert [(c.kind, c.reason) for c in lane.changes()][-1] == (ChangeKind.DROPPED, EndReason.EXPIRED)


def test_a_newer_revision_waits_in_its_place_and_an_older_one_is_ignored() -> None:
    lane = _lane(_segment("1"), _segment("2", "PASS +9"), _segment("3", "SACK -7"))
    _air(lane, T0)
    lane.offer([_segment("2", "PASS +12", revision=1), _segment("3", "SACK -9", revision=0)])
    assert _air(lane, T0 + MIN) == ("PASS +12", "PASS +12 detail")
    assert _air(lane, T0 + 2 * MIN) == ("SACK -7", "SACK -7 detail")


def test_an_edit_on_air_swaps_the_frames_and_keeps_the_dwell() -> None:
    lane = _lane(_segment("1", "RUN +4"))
    _air(lane, T0)
    _air(lane, T0 + MAX)  # the players' frame, since T0 + MAX
    lane.offer([_segment("1", "RUN +6", players=None, revision=1)])  # one frame now: clamp to it
    assert _air(lane, T0 + MAX + 5 * S) == ("RUN +6", "RUN +6 detail")
    assert _air(lane, T0 + 2 * MAX - S) == ("RUN +6", "RUN +6 detail")
    assert _air(lane, T0 + 2 * MAX) is None  # the dwell from before the edit
    lane.offer([_segment("9", "PUNT", players=None), _segment("1", "RUN +7", revision=2)])
    assert _air(lane, T0 + 3 * MAX) == ("PUNT", "PUNT detail")  # an edit to one already gone doesn't bring it back
    assert _air(lane, T0 + 4 * MAX) is None


def test_a_lane_takes_only_its_own_surface() -> None:
    with pytest.raises(ValueError, match="board segment offered to the play_strip lane"):
        _lane(_segment("1", surface=Surface.BOARD))


def test_the_next_deadline_is_when_the_lane_would_change_by_itself() -> None:
    lane = _lane()
    assert lane.next_deadline(_at(T0)) is None  # idle
    lane.offer([_segment("1", available=T0 + 5 * S)])
    assert lane.next_deadline(_at(T0)) == MonotonicTime(5.0)  # when it becomes available
    cue = lane.cue(_at(T0 + 5 * S))
    assert cue is not None and lane.next_deadline(_at(T0 + 5 * S)) is None  # waiting for its commit
    lane.committed(cue, _at(T0 + 6 * S))
    assert lane.next_deadline(_at(T0 + 6 * S)) == MonotonicTime(26.0)  # its maximum
    lane.offer([_segment("2", available=T0 + 30 * S)])
    assert lane.next_deadline(_at(T0 + 6 * S)) == MonotonicTime(26.0)  # the maximum comes first
    lane.offer([_segment("3")])
    lane.invalidate()
    assert lane.next_deadline(_at(T0 + 6 * S)) == MonotonicTime(16.0)  # pending: its minimum

    held = _lane(_stoppage("t", "TV TIMEOUT"))
    _air(held, T0)
    assert held.next_deadline(_at(T0)) is None  # held: only something new ends it
    held.offer([_segment("4", available=T0 + 4 * S)])
    assert held.next_deadline(_at(T0)) == MonotonicTime(10.0)  # available at 4 s, but the minimum is 10 s
    expiring = _lane(_segment("6", expires_at=T0 + 7 * S))
    expiring.cue(_at(T0))
    assert expiring.next_deadline(_at(T0)) == MonotonicTime(7.0)


def test_earliest_skips_what_isnt_set() -> None:
    assert earliest([None, MonotonicTime(3.0), MonotonicTime(1.0)]) == MonotonicTime(1.0)
    assert earliest([None]) is None and earliest([]) is None


def test_a_segment_and_its_dwell_are_checked_when_made() -> None:
    with pytest.raises(ValueError, match="a dwell"):
        Dwell(minimum=-S, maximum=S)
    with pytest.raises(ValueError, match="a dwell"):
        Dwell(minimum=2 * S, maximum=S)
    good = _segment("1")
    with pytest.raises(ValueError, match="at least one frame"):
        replace(good, frames=())
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(good, available_at=datetime(2026, 11, 16))
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(good, provenance=Provenance(SOURCE, received_at=T0, as_of=datetime(2026, 11, 16)))
    with pytest.raises(ValueError, match="latest start"):
        replace(good, latest_start=T0 - S)
    with pytest.raises(ValueError, match="expire after"):
        replace(good, expires_at=T0)
    assert Cue(good, 1).payload.detail == "C.Brown 4 YDS 1 CAR"
