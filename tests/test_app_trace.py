"""Basic decision traces: one line per change in the source, or in what the board put up and took down."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from omni.app.loop import Painted, Presentation, Refresh
from omni.app.trace import DecisionTracer
from omni.cards.base import CardId
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.ids import SourceRef
from omni.queue.scheduler import SelectionReason
from omni.tower.board import BoardFrame, BoardHealth, BoardPayload, FrameKind, SourceHealth
from omni.tower.lane import ChangeKind, EndReason, LaneChange
from omni.tower.segment import Dwell, Provenance, Segment, SegmentKey, Surface
from tests.test_app_loop import T, _at, _build

LATER = T + timedelta(seconds=12)


def _refresh() -> Refresh:
    loop, _, _ = _build(lag=0)
    return loop.refresh(T)


def _first_paint() -> Painted:
    loop, _, _ = _build(lag=0)
    loop.refresh(T)
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


def test_a_card_going_up_names_why_and_how_its_teams_were_drawn() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    painted = _first_paint()
    tracer.painted(painted)
    assert painted.presented is not None and len(lines) == 1
    up = lines[0]
    assert up.startswith(f"up 23:00:00: {painted.presented.card.raw} (rotation) | away base bar #")
    assert " meter #" in up and up.endswith("| contrast 3/25")


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
