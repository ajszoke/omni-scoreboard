"""The frame stream: the wire to the display process, its word back, and the sink that credits only that word."""

from __future__ import annotations

import io
import struct
import threading
from datetime import timedelta

import pytest

from omni.app.frame_stream import (
    FRAME_MAGIC,
    LEGACY_FRAME_MAGIC,
    READY_LINE,
    BackgroundFrameWriter,
    DisplayLink,
    DisplayLost,
    DisplayUnavailable,
    FrameShown,
    LinkProtocol,
    LossCause,
    StreamDisplaySink,
    StreamFrame,
    ack_line,
    encode_frame,
    encode_legacy_frame,
    read_replies,
)
from omni.core.clock import MonotonicTime
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.renderers.canvas import Canvas, RecordingCanvas
from tests.test_frame_writer import _SOON, _until

RED = RGBColor(255, 0, 0)


def _frame(frame_id: int) -> StreamFrame:
    return StreamFrame(frame_id, 2, 1, bytes(6))


def _lit(sink: StreamDisplaySink, x: int = 0) -> Canvas:
    """A frame with one pixel lit, at ``x``: frames lit at the same place are identical."""
    frame = sink.new_frame()
    frame.set_pixel(x, 0, RED)
    return frame


# The wire.


def test_a_frame_goes_out_with_its_id_or_without_it_to_an_older_display_process() -> None:
    frame = StreamFrame(7, 64, 32, bytes(64 * 32 * 3))
    sent = encode_frame(frame)
    assert sent[:4] == FRAME_MAGIC and struct.unpack("<IHH", sent[4:12]) == (7, 64, 32)
    assert len(sent) == 12 + 64 * 32 * 3
    legacy = encode_legacy_frame(frame)
    assert legacy[:4] == LEGACY_FRAME_MAGIC and struct.unpack("<HH", legacy[4:8]) == (64, 32)
    assert legacy[8:] == sent[12:]
    with pytest.raises(ValueError, match="three bytes per pixel"):
        StreamFrame(1, 2, 2, b"\x00")
    with pytest.raises(ValueError, match="32-bit"):
        StreamFrame(0, 2, 1, bytes(6))
    with pytest.raises(ValueError, match="32-bit"):
        StreamFrame(2**32, 2, 1, bytes(6))


def test_the_replies_say_the_display_process_is_ready_then_which_frames_it_showed() -> None:
    ready: list[bool] = []
    shown: list[int] = []
    replies = io.BytesIO(
        b"Suppressed: some library chatter\n" + READY_LINE + ack_line(1) + b"ACK two\n" + b"ACK 3 4\n" + ack_line(12)
    )
    read_replies(replies, ready=lambda: ready.append(True), shown=shown.append)
    assert ready == [True] and shown == [1, 12]  # what isn't a reply is ignored


# The link: word from the display processes.


def test_only_the_current_display_process_is_heard() -> None:
    link = DisplayLink()
    link.shown(1, 1)  # nothing started yet
    link.started(1)
    link.ready(1)
    link.shown(1, 2)
    link.started(2)  # the first is gone: its panel no longer shows frame 2
    link.ready(1)
    link.shown(1, 3)  # an old acknowledgement, arriving after the reconnect
    assert not link.acknowledges(1) and not link.acknowledges(2)  # the new one hasn't said it's ready
    link.ready(2)
    link.shown(2, 4)
    assert link.acknowledges(2)
    assert link.receipts() == [FrameShown(2, generation=1), DisplayLost(1), FrameShown(4, generation=2)]
    assert link.receipts() == []


def test_a_display_process_that_ends_without_showing_anything_is_no_loss() -> None:
    link = DisplayLink()
    link.started(1)
    link.ended(2)  # not the current one
    link.ended(1)
    link.started(2)
    link.ready(2)
    link.shown(2, 1)
    link.ended(2)
    assert link.receipts() == [FrameShown(1, generation=2), DisplayLost(2)]


def test_a_display_process_that_never_answers_is_credited_unconfirmed_once_frames_have_gone_to_it_a_while() -> None:
    now = [100.0]
    link = DisplayLink(unconfirmed_after=timedelta(seconds=5), monotonic=lambda: now[0])
    link.written(1, 1)  # nothing started yet
    link.started(1)
    link.written(1, 2)
    now[0] = 104.9
    link.written(1, 3)
    assert link.receipts() == []
    now[0] = 105.0
    link.written(1, 4)
    link.written(2, 5)  # not the current one
    assert link.receipts() == [FrameShown(4, generation=1, confirmed=False)]
    link.started(2)  # a new one gets its own few seconds
    link.written(2, 6)
    assert link.receipts() == [DisplayLost(1)]


def test_a_display_process_that_acknowledges_frames_is_never_credited_without_its_word() -> None:
    now = [0.0]
    link = DisplayLink(monotonic=lambda: now[0])
    link.started(1)
    link.ready(1)
    for second in range(0, 30):
        now[0] = float(second)
        link.written(1, second + 1)
    assert link.receipts() == []
    link.shown(1, 30)
    assert link.receipts() == [FrameShown(30, generation=1)]


def test_a_frame_like_the_one_on_the_panel_is_vouched_for_and_any_other_is_not() -> None:
    link = DisplayLink()
    assert link.vouch(2, like=1) is False  # no display at all
    link.started(1)
    link.ready(1)
    assert link.vouch(2, like=1) is False  # it hasn't shown anything
    link.shown(1, 1)
    assert link.vouch(3, like=2) is False  # it doesn't show the frame this one copies
    assert link.vouch(4, like=1) is True
    assert link.vouch(5, like=4) is True  # and a copy of that copy
    link.started(2)
    assert link.vouch(6, like=5) is False  # a new display process shows nothing yet
    assert link.receipts() == [
        FrameShown(1, generation=1),
        FrameShown(4, generation=1),
        FrameShown(5, generation=1),
        DisplayLost(1),
    ]


# The sink: frames handed over, and only the display's word counts.


class _Blocked:
    """A write to the display process that waits for the test, then lands; the display acknowledges it."""

    def __init__(self, link: DisplayLink) -> None:
        self.link = link
        self.writing = threading.Event()
        self.release = threading.Event()
        self.written: list[int] = []

    def __call__(self, frame: StreamFrame) -> bool:
        self.writing.set()
        assert self.release.wait(_SOON)
        self.written.append(frame.id)
        self.link.shown(1, frame.id)
        return True


def _acknowledging(link: DisplayLink) -> DisplayLink:
    link.started(1)
    link.ready(1)
    return link


def test_a_frame_handed_over_while_the_writer_is_blocked_is_not_shown_until_the_display_says_so() -> None:
    link = _acknowledging(DisplayLink())
    write = _Blocked(link)
    writer = BackgroundFrameWriter(write, on_error=lambda exc: None)
    sink = StreamDisplaySink(PanelProfile.SINGLE_64X32, writer, link)
    writer.start()
    first = sink.send(_lit(sink))
    assert write.writing.wait(_SOON)  # it's going out, and the display is stalled
    assert link.receipts() == []  # handed over is not shown
    write.release.set()
    assert _until(lambda: write.written == [first])
    assert link.receipts() == [FrameShown(first, generation=1)]
    writer.stop(_SOON)


def test_a_frame_replaced_before_it_went_out_is_never_shown() -> None:
    link = _acknowledging(DisplayLink())
    write = _Blocked(link)
    writer = BackgroundFrameWriter(write, on_error=lambda exc: None)
    sink = StreamDisplaySink(PanelProfile.SINGLE_64X32, writer, link)
    writer.start()
    first = sink.send(_lit(sink, 0))
    assert write.writing.wait(_SOON)
    replaced = sink.send(_lit(sink, 1))
    newest = sink.send(_lit(sink, 2))
    write.release.set()
    assert _until(lambda: write.written == [first, newest])
    shown = link.receipts()
    assert shown == [FrameShown(first, generation=1), FrameShown(newest, generation=1)]
    assert all(receipt.frame != replaced for receipt in shown if isinstance(receipt, FrameShown))
    writer.stop(_SOON)


class _Pipe:
    """A writer that keeps every frame handed to it, and reports it going out unless told otherwise."""

    def __init__(self) -> None:
        self.frames: list[StreamFrame] = []
        self.taking = True

    def __call__(self, frame: StreamFrame) -> bool:
        self.frames.append(frame)
        return self.taking


def test_a_duplicate_goes_out_again_unless_the_display_shows_the_frame_it_copies() -> None:
    link = _acknowledging(DisplayLink())
    pipe = _Pipe()
    sink = StreamDisplaySink(PanelProfile.SINGLE_64X32, pipe, link)
    assert sink.profile is PanelProfile.SINGLE_64X32
    first = sink.send(_lit(sink))
    again = sink.send(_lit(sink))  # identical, but the display hasn't shown the first yet
    assert [frame.id for frame in pipe.frames] == [first, again]
    assert pipe.frames[1].pixels == pipe.frames[0].pixels
    assert link.receipts() == []  # neither is shown: a duplicate isn't credited before the display's word
    link.shown(1, again)
    copy = sink.send(_lit(sink))  # the panel shows these pixels: nothing to send, and shown at once
    assert len(pipe.frames) == 2
    assert link.receipts() == [FrameShown(again, generation=1), FrameShown(copy, generation=1)]

    stalled = sink.send(_lit(sink, 5))  # a change goes out, and the display goes quiet
    duplicate = sink.send(_lit(sink, 5))  # its copy isn't shown just because the last was
    back = sink.send(_lit(sink))  # nor is a copy of what the panel showed before the change
    assert [frame.id for frame in pipe.frames[2:]] == [stalled, duplicate, back]
    assert link.receipts() == []

    link.shown(1, back)
    link.started(2)  # a new display process: a blank panel
    link.ready(2)
    fresh = sink.send(_lit(sink))
    assert pipe.frames[-1].id == fresh
    assert link.receipts() == [FrameShown(back, generation=1), DisplayLost(1)]


def test_a_frame_the_writer_says_is_not_going_out_is_reported_and_sent_again() -> None:
    link = _acknowledging(DisplayLink())
    pipe = _Pipe()
    sink = StreamDisplaySink(PanelProfile.SINGLE_64X32, pipe, link)
    pipe.taking = False
    with pytest.raises(DisplayUnavailable):
        sink.commit(_lit(sink))  # the writer is failing or stalled: the board learns at once
    pipe.taking = True
    sink.commit(_lit(sink))  # the same pixels go out again: the display never showed them
    assert len(pipe.frames) == 2 and pipe.frames[0].pixels == pipe.frames[1].pixels
    assert sink.receipts() == []
    with pytest.raises(TypeError):
        sink.send(RecordingCanvas(64, 32))


# Sessions: a receiver that reads them, its heartbeats and its watchdog.


def _sessions(now: list[float]) -> DisplayLink:
    link = DisplayLink(monotonic=lambda: now[0])
    link.started(1)
    link.ready(1)
    link.announced(1, "boot-a")
    return link


def test_a_receiver_that_reads_sessions_is_credited_only_for_the_current_session() -> None:
    now = [0.0]
    link = _sessions(now)
    protocol, session = link.protocol(1)
    assert protocol is LinkProtocol.SESSIONS and link.acknowledges(1)
    assert link.protocol(2) == (LinkProtocol.UNCONFIRMED, session)  # not a generation that's running
    link.shown(1, 1, session=session + 1)  # a session it isn't in
    link.shown(2, 2, session=session)  # another generation's word
    now[0] = 1.5
    link.shown(1, 3, session=session)
    [shown] = link.receipts()
    assert shown == FrameShown(3, generation=1) and shown.session == session and shown.heard == MonotonicTime(1.5)


def test_a_blank_ends_the_session_and_a_late_ack_from_it_renews_nothing() -> None:
    now = [0.0]
    link = _sessions(now)
    _, session = link.protocol(1)
    link.shown(1, 1, session=session)
    link.blanked(1, session + 5)  # not its session: ignored
    link.blanked(2, session)  # not its generation: ignored
    link.blanked(1, session)
    receipts = link.receipts()
    assert receipts == [FrameShown(1, generation=1), DisplayLost(1)]
    lost = receipts[1]
    assert isinstance(lost, DisplayLost) and lost.cause is LossCause.BLANKED and lost.session == session
    assert link.protocol(1) == (LinkProtocol.SESSIONS, session + 1)  # the next frames go to a new session
    link.shown(1, 2, session=session)  # the old session's ack arrives late
    assert link.receipts() == [] and link.vouch(3, like=1) is False
    link.shown(1, 4, session=session + 1)
    assert link.receipts() == [FrameShown(4, generation=1)]


def test_a_receiver_that_restarts_under_the_same_transport_starts_a_new_session() -> None:
    now = [0.0]
    link = _sessions(now)
    _, session = link.protocol(1)
    link.announced(2, "boot-z")  # another generation: ignored
    link.announced(1, "boot-a")  # the same run, said again: nothing changes
    link.shown(1, 1, session=session)
    link.announced(1, "boot-b")
    receipts = link.receipts()
    assert receipts == [FrameShown(1, generation=1), DisplayLost(1)]
    assert isinstance(receipts[1], DisplayLost) and receipts[1].cause is LossCause.REBOOTED
    assert link.protocol(1)[1] == session + 1


def test_a_receiver_silent_past_its_watchdog_has_lost_its_panel_while_heartbeats_keep_it() -> None:
    now = [0.0]
    link = _sessions(now)
    _, session = link.protocol(1)
    link.shown(1, 1, session=session)
    for second in (2.0, 4.0, 6.0, 8.0, 10.0):
        now[0] = second
        link.alive(1, session)
        link.alive(1, session + 1)  # not its session: no word
        link.alive(2, session)  # nor another generation's
    assert link.receipts() == [FrameShown(1, generation=1)]
    now[0] = 17.9
    assert link.receipts() == []
    now[0] = 18.0
    receipts = link.receipts()
    assert receipts == [DisplayLost(1)] and isinstance(receipts[0], DisplayLost)
    assert receipts[0].cause is LossCause.SILENT and receipts[0].heard == MonotonicTime(18.0)
    now[0] = 30.0
    assert link.receipts() == []  # nothing more to lose


def test_an_older_receiver_never_goes_silent_and_keeps_its_acks() -> None:
    now = [0.0]
    link = DisplayLink(monotonic=lambda: now[0])
    link.started(1)
    link.ready(1)
    link.shown(1, 1)
    now[0] = 3600.0
    assert link.receipts() == [FrameShown(1, generation=1)]
    assert link.protocol(1)[0] is LinkProtocol.ACKNOWLEDGING
    link.shown(1, 2, session=1)  # it can't speak sessions: such a word isn't its
    assert link.receipts() == []


def test_an_unchanged_frame_asks_for_a_heartbeat_every_2_s_and_a_changed_one_counts_as_one() -> None:
    now = [0.0]
    link = _sessions(now)
    pipe = _Pipe()
    beats: list[float] = []
    sink = StreamDisplaySink(
        PanelProfile.SINGLE_64X32, pipe, link, beat=lambda: beats.append(now[0]), monotonic=lambda: now[0]
    )
    first = sink.send(_lit(sink))
    link.shown(1, first, session=link.protocol(1)[1])
    for tenth in range(1, 51):  # five seconds of the same frame, drawn ten times a second
        now[0] = tenth / 10
        sink.send(_lit(sink))
    assert beats == [2.0, 4.0]
    now[0] = 5.5
    sink.send(_lit(sink, 3))  # a change goes out: it's as good as a heartbeat
    assert len(pipe.frames) == 2
    plain = StreamDisplaySink(PanelProfile.SINGLE_64X32, _Pipe(), _sessions([0.0]))
    plain.send(_lit(plain))  # without a beat to ask, nothing is asked
