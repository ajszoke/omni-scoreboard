"""Basic decision traces: one line per change in the source, or in what the board put up and took down."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from omni.app.loop import Painted, Presentation, Refresh
from omni.app.trace import DecisionTracer
from omni.cards.base import CardId, EndBasis, GameEnd
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.ids import SourceRef
from omni.queue.scheduler import Missed, MissReason, SelectionReason
from omni.tower.board import BoardFrame, BoardHealth, BoardNote, BoardPayload, FrameKind, SourceHealth
from omni.tower.lane import ChangeKind, EndReason, LaneChange
from omni.tower.segment import Dwell, Provenance, Segment, SegmentKey, Surface
from tests.test_app_loop import T, _at, _build, _game

LATER = T + timedelta(seconds=12)


def _refresh() -> Refresh:
    loop, _, _ = _build(lag=0)
    return loop.refresh(_at(T))


def _first_paint() -> Painted:
    loop, _, _ = _build(lag=0)
    loop.refresh(_at(T))
    return loop.paint(_at(T))


def _frame_change(kind: ChangeKind, frame: BoardFrame) -> LaneChange[BoardPayload]:
    segment: Segment[BoardPayload] = Segment(
        key=SegmentKey(Surface.BOARD, f"frame:{frame.kind.value}"),
        frames=(frame,),
        dwell=Dwell(minimum=timedelta(0), maximum=timedelta(0)),
        provenance=Provenance(SourceRef("board"), received_at=T),
        available_at=T,
    )
    return LaneChange(kind, segment, ClockReading(T, MonotonicTime(0.0)))


def test_a_refresh_is_written_when_the_source_changes_and_names_the_next_one() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    refresh = _refresh()
    tracer.refreshed(refresh, took=timedelta(milliseconds=420), next_at=LATER)
    tracer.refreshed(dataclasses.replace(refresh, at=LATER), took=timedelta(0), next_at=LATER)  # no change
    assert lines == ["refresh 23:00:00 in 0.4s: source fresh | next 23:00:12"]
    stale = BoardHealth(source=SourceHealth.STALE, reason="no schedule for over 2m: down", held=2)
    tracer.refreshed(dataclasses.replace(refresh, health=stale), took=timedelta(0), next_at=None)
    assert lines[-1] == "refresh 23:00:00 in 0.0s: source stale (no schedule for over 2m: down) | held 2 | next -"


def test_a_refresh_that_drops_a_game_or_a_play_is_always_written() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    refresh = _refresh()
    tracer.refreshed(refresh, took=timedelta(0), next_at=LATER)
    pipeline = dataclasses.replace(refresh.pipeline, skipped=("g2: feed down",), warnings=("g1: odd play",))
    tracer.refreshed(dataclasses.replace(refresh, pipeline=pipeline), took=timedelta(0), next_at=LATER)
    assert lines[1:] == [lines[0], "  dropped: g2: feed down", "  dropped: g1: odd play"]


def test_a_card_that_left_before_it_aired_is_written_with_why() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    loop, _, _ = _build(lag=0)
    refresh = loop.refresh(_at(T))
    card = loop._queue.card("mlb:mlb_statsapi:g1:live")
    assert card is not None
    tracer.refreshed(refresh, took=timedelta(0), next_at=LATER)
    unseen = (Missed(card, MissReason.TOO_LATE), Missed(card, MissReason.EXPIRED), Missed(card, MissReason.UNDRAWN))
    tracer.refreshed(dataclasses.replace(refresh, unseen=unseen), took=timedelta(0), next_at=LATER)
    assert lines[1:] == [
        lines[0],
        "  unseen: mlb:mlb_statsapi:g1:live (too late to start)",
        "  unseen: mlb:mlb_statsapi:g1:live (expired before it aired)",
        "  unseen: mlb:mlb_statsapi:g1:live (failed to draw)",
    ]


def test_a_final_revealed_is_written_with_when_its_game_ended_and_on_what_basis() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    refresh = _refresh()
    tracer.refreshed(refresh, took=timedelta(0), next_at=LATER)
    ends = (
        (CardId("mlb:mlb_statsapi:g1:final"), GameEnd(T - timedelta(seconds=40), EndBasis.DECISIVE_PLAY)),
        (CardId("mlb:mlb_statsapi:g2:final"), GameEnd(T, EndBasis.FIRST_SEEN)),
    )
    pipeline = dataclasses.replace(refresh.pipeline, ends=ends)
    tracer.refreshed(dataclasses.replace(refresh, pipeline=pipeline), took=timedelta(0), next_at=LATER)
    assert lines[1:] == [
        lines[0],
        "  final: mlb:mlb_statsapi:g1:final ended 22:59:20 (its decisive play)",
        "  final: mlb:mlb_statsapi:g2:final ended 23:00:00 (first seen final)",
    ]


def test_a_corrected_final_and_a_final_its_feed_hasnt_confirmed_are_written() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    refresh = _refresh()
    tracer.refreshed(refresh, took=timedelta(0), next_at=LATER)
    waiting = dataclasses.replace(refresh.pipeline, unconfirmed=(_game("g2").id,))
    tracer.refreshed(dataclasses.replace(refresh, pipeline=waiting), took=timedelta(0), next_at=LATER)
    tracer.refreshed(dataclasses.replace(refresh, pipeline=waiting), took=timedelta(0), next_at=LATER)  # no change
    corrected = dataclasses.replace(refresh.pipeline, corrections=(CardId("mlb:mlb_statsapi:g1:final"),))
    tracer.refreshed(dataclasses.replace(refresh, pipeline=corrected), took=timedelta(0), next_at=LATER)
    assert lines[1:] == [
        lines[0],
        "  unconfirmed: mlb:mlb_statsapi:g2 (final on the schedule; its feed hasn't said so)",
        lines[0],
        "  corrected: mlb:mlb_statsapi:g1:final",
    ]


def test_a_board_note_is_written_when_the_paint_made_it() -> None:
    lines: list[str] = []
    DecisionTracer(lines.append, timezone=timezone.utc).painted(
        Painted(at=LATER, shown=CardId("mlb:mlb_statsapi:g1:bigplay:g1:ab:3"), notes=(BoardNote.NO_BASE_CARD,))
    )
    assert lines == ["note 23:00:12: no base card for the recovery turn"]


def test_a_card_going_up_names_why_and_how_its_teams_were_drawn() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    painted = _first_paint()
    tracer.painted(painted)
    assert painted.presented is not None and len(lines) == 1
    up = lines[0]
    assert up.startswith(f"up 23:00:00: {painted.presented.card.raw} (rotation) | away base bar #")
    assert " meter #" in up and up.endswith("| contrast 4.5/25|20")


def test_a_card_without_two_team_rows_goes_up_without_a_treatment() -> None:
    lines: list[str] = []
    painted = _first_paint()
    assert painted.presented is not None
    presented = dataclasses.replace(painted.presented, card=CardId("golf:live"), treatment=None)
    DecisionTracer(lines.append, timezone=timezone.utc).painted(dataclasses.replace(painted, presented=presented))
    assert lines[-1] == "up 23:00:00: golf:live (rotation)"


def test_frames_downs_and_drops_are_written_with_their_reasons() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    held = BoardFrame(FrameKind.HELD, "2 games in the TV delay")
    painted = _first_paint()
    card_up = next(change for change in painted.changes if change.kind is ChangeKind.UP)
    down = dataclasses.replace(
        card_up, kind=ChangeKind.DOWN, reason=EndReason.RAN_ITS_TIME, aired=timedelta(seconds=12)
    )
    dropped = dataclasses.replace(card_up, kind=ChangeKind.DROPPED, reason=EndReason.INVALIDATED)
    changes = (_frame_change(ChangeKind.UP, held), down, dropped, _frame_change(ChangeKind.DROPPED, held))
    tracer.painted(Painted(at=T, shown=None, frame=FrameKind.HELD, changes=changes))
    card = painted.shown.raw if painted.shown is not None else "-"
    assert lines == [
        "frame 23:00:00: held (2 games in the TV delay)",
        f"down 23:00:00: {card} (ran its time, 12.0s)",
        f"drop 23:00:00: {card} (no longer current)",
        "drop 23:00:00: held (-)",
    ]


def test_a_card_turning_stale_is_written_once() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    stale = Painted(at=LATER, shown=CardId("mlb:live"), stale=True)
    tracer.painted(stale)
    tracer.painted(stale)
    tracer.painted(dataclasses.replace(stale, stale=False))
    tracer.painted(stale)
    assert lines == ["stale 23:00:12: mlb:live", "stale 23:00:12: mlb:live"]


def test_a_paint_failure_is_written_when_it_starts_and_when_frames_commit_again() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    failed = Painted(at=T, shown=None, render_error="RenderDispatchError: no renderer")
    tracer.painted(failed)
    tracer.painted(failed)
    tracer.painted(Painted(at=T, shown=None, frame=FrameKind.IDLE))
    tracer.painted(Painted(at=T, shown=CardId("mlb:live")))
    assert lines == ["paint failed: RenderDispatchError: no renderer", "paint recovered"]


def test_a_refresh_that_breaks_is_written() -> None:
    lines: list[str] = []
    DecisionTracer(lines.append).failed(RuntimeError("boom"))
    assert lines == ["refresh failed: RuntimeError: boom"]


def test_times_read_in_the_board_zone() -> None:
    from zoneinfo import ZoneInfo

    lines: list[str] = []
    DecisionTracer(lines.append, timezone=ZoneInfo("America/Denver")).refreshed(
        _refresh(), took=timedelta(0), next_at=datetime(2026, 6, 17, 23, 0, 12, tzinfo=timezone.utc)
    )
    assert lines[0].startswith("refresh 17:00:00") and lines[0].endswith("next 17:00:12")


def test_presentations_carry_the_queue_s_reason() -> None:
    painted = _first_paint()
    assert isinstance(painted.presented, Presentation) and painted.presented.reason is SelectionReason.ROTATION


def test_games_with_no_feed_are_counted_and_a_card_up_without_its_presentation_still_shows() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    tracer.refreshed(dataclasses.replace(_refresh(), health=BoardHealth(unfed=1)), took=timedelta(0), next_at=None)
    assert lines == ["refresh 23:00:00 in 0.0s: source fresh | unfed 1 | next -"]
    painted = _first_paint()
    tracer.painted(dataclasses.replace(painted, presented=None))
    assert lines[-1] == f"up 23:00:00: {painted.shown.raw if painted.shown is not None else '-'}"
