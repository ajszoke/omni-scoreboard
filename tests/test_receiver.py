"""The receiver's protocol (`omni.app.receiver`): the wire, the replies, and the receiver that blanks on its own."""

from __future__ import annotations

import io
import struct

import pytest

from omni.app.frame_stream import StreamFrame, encode_frame, encode_legacy_frame
from omni.app.receiver import (
    FRAME_MAGIC_V3,
    HEARTBEAT_MAGIC,
    READY_LINE,
    WATCHDOG,
    Frame,
    Heartbeat,
    OutOfSync,
    Receiver,
    Reply,
    ReplyKind,
    encode_heartbeat,
    encode_session_frame,
    parse_reply,
    read_message,
)

PIXELS = bytes(range(6))


def _frame(session: int, frame_id: int) -> Frame:
    return Frame(2, 1, PIXELS, frame_id, session)


# The wire.


def test_each_kind_of_message_reads_back_as_written_and_a_stream_cut_short_ends_the_messages() -> None:
    v3 = encode_session_frame(7, 41, 2, 1, PIXELS)
    assert v3[:4] == FRAME_MAGIC_V3 and struct.unpack("<IIHH", v3[4:16]) == (7, 41, 2, 1)
    beat = encode_heartbeat(7)
    assert beat == HEARTBEAT_MAGIC + struct.pack("<I", 7)
    older = encode_frame(StreamFrame(42, 2, 1, PIXELS))
    oldest = encode_legacy_frame(StreamFrame(43, 2, 1, PIXELS))
    stream = io.BytesIO(v3 + beat + older + oldest)
    assert read_message(stream) == _frame(7, 41)
    assert read_message(stream) == Heartbeat(7)
    assert read_message(stream) == Frame(2, 1, PIXELS, 42, None)
    assert read_message(stream) == Frame(2, 1, PIXELS, None, None)
    assert read_message(stream) is None
    for message in (v3, beat, older, oldest):
        for cut in (6, len(message) - 1):
            assert read_message(io.BytesIO(message[:cut])) is None
    assert read_message(io.BytesIO(v3[:4])) is None  # the magic alone


def test_a_stream_that_loses_its_place_says_so() -> None:
    with pytest.raises(OutOfSync, match="no message starts"):
        read_message(io.BytesIO(b"JUNKmore"))


def test_each_reply_reads_back_as_written_and_anything_else_is_ignored() -> None:
    replies = [
        Reply(ReplyKind.READY_V2),
        Reply(ReplyKind.READY, boot="9f2c"),
        Reply(ReplyKind.SHOWN, session=3, frame=12),
        Reply(ReplyKind.ACK, frame=12),
        Reply(ReplyKind.ALIVE, session=3),
        Reply(ReplyKind.BLANK, session=3),
    ]
    assert [reply.line() for reply in replies] == [
        READY_LINE,
        b"OMN3 READY 9f2c\n",
        b"SHOWN 3 12\n",
        b"ACK 12\n",
        b"ALIVE 3\n",
        b"BLANK 3\n",
    ]
    assert [parse_reply(reply.line()) for reply in replies] == replies
    for chatter in (b"Suppressed: library chatter\n", b"ACK two\n", b"ACK 3 4\n", b"SHOWN 3\n", b"ALIVE x\n", b"\n"):
        assert parse_reply(chatter) is None


# The receiver.


def test_it_says_it_is_ready_to_an_older_app_and_to_a_new_one_with_its_boot_id() -> None:
    assert Receiver("b0").hello() == [Reply(ReplyKind.READY_V2), Reply(ReplyKind.READY, boot="b0")]


def test_a_frame_waits_for_the_panel_one_at_a_time_and_a_replaced_one_is_never_acknowledged() -> None:
    receiver = Receiver("b0")
    assert not receiver.waiting and receiver.take() is None
    assert receiver.heard(_frame(1, 1), 0.0) == []
    assert receiver.heard(_frame(1, 2), 0.1) == []  # it replaces frame 1, which never goes up
    assert receiver.waiting
    shown = receiver.take()
    assert shown == _frame(1, 2) and not receiver.waiting
    assert receiver.swapped(shown) == [Reply(ReplyKind.SHOWN, session=1, frame=2)]


def test_an_older_app_s_frames_are_acknowledged_as_it_reads_them() -> None:
    receiver = Receiver("b0")
    assert receiver.swapped(Frame(2, 1, PIXELS, 5, None)) == [Reply(ReplyKind.ACK, frame=5)]
    assert receiver.swapped(Frame(2, 1, PIXELS)) == []  # an OMNF frame has no id to acknowledge


def test_a_frame_or_heartbeat_from_an_older_session_is_ignored_and_a_newer_session_takes_over() -> None:
    receiver = Receiver("b0")
    receiver.heard(_frame(2, 1), 0.0)
    assert receiver.heard(_frame(1, 2), 0.1) == []  # the app has moved on from session 1
    assert receiver.heard(Heartbeat(1), 0.2) == []
    assert receiver.take() == _frame(2, 1)
    receiver.heard(_frame(2, 3), 0.3)
    assert receiver.heard(Heartbeat(3), 0.4) == [Reply(ReplyKind.ALIVE, session=3)]
    assert receiver.take() is None  # session 2's frame was dropped when session 3 began
    receiver.heard(_frame(3, 4), 0.5)
    receiver.heard(_frame(4, 5), 0.6)
    assert receiver.take() == _frame(4, 5)


def test_it_blanks_after_8_s_without_a_heartbeat_even_while_the_frame_is_unchanged_and_says_so() -> None:
    receiver = Receiver("b0")
    assert receiver.deadline() is None  # nothing on the panel
    receiver.heard(_frame(1, 1), 10.0)
    receiver.swapped(receiver.take() or _frame(0, 0))
    for second in (12.0, 14.0, 16.0):  # the frame doesn't change; the app's heartbeats keep it up
        assert receiver.heard(Heartbeat(1), second) == [Reply(ReplyKind.ALIVE, session=1)]
        assert receiver.expired(second) == []
    assert receiver.deadline() == 16.0 + WATCHDOG
    assert receiver.expired(23.9) == []  # the app has stopped: no heartbeat since 16 s
    receiver.heard(_frame(0, 9), 23.95)  # an older session's frame keeps nothing alive
    assert receiver.expired(24.0) == [Reply(ReplyKind.BLANK, session=1)]
    assert receiver.deadline() is None and receiver.expired(60.0) == []  # blank: nothing more to blank
    receiver.heard(_frame(2, 2), 61.0)  # the app comes back with a new session
    assert receiver.swapped(receiver.take() or _frame(0, 0)) == [Reply(ReplyKind.SHOWN, session=2, frame=2)]
    assert receiver.deadline() == 61.0 + WATCHDOG


def test_a_frame_waiting_when_the_watchdog_fires_never_goes_up() -> None:
    receiver = Receiver("b0", watchdog=1.0)
    receiver.heard(_frame(1, 1), 0.0)
    receiver.swapped(receiver.take() or _frame(0, 0))
    receiver.heard(_frame(1, 2), 0.5)
    assert receiver.expired(1.5) == [Reply(ReplyKind.BLANK, session=1)]
    assert receiver.take() is None


def test_an_older_app_never_arms_the_watchdog() -> None:
    receiver = Receiver("b0")
    receiver.heard(Frame(2, 1, PIXELS, 1, None), 0.0)
    receiver.swapped(receiver.take() or _frame(0, 0))
    assert receiver.deadline() is None and receiver.expired(3600.0) == []
