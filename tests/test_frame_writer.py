"""Writing frames to the display process: `BackgroundFrameWriter` writes on its own thread, newest first,
retried until they land; `SinkProcess` runs the display process and reads its word back."""

from __future__ import annotations

import io
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import IO, cast

from omni.app.frame_stream import (
    READY_LINE,
    BackgroundFrameWriter,
    ChildProcess,
    DisplayLink,
    DisplayLost,
    DisplayReceipt,
    FrameShown,
    LinkProtocol,
    LossCause,
    SinkProcess,
    StreamFrame,
    ack_line,
    encode_frame,
    encode_legacy_frame,
)
from omni.app.receiver import encode_heartbeat, encode_session_frame

_SOON = 5.0  # seconds a test waits for the writer before calling it stuck


def _until(condition: Callable[[], bool], *, within: float = _SOON) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.001)
    return False


def _never(exc: Exception) -> None:
    raise AssertionError(f"the writer reported {exc!r}")


def test_handing_over_a_frame_never_waits_and_the_newest_one_goes_out() -> None:
    written: list[bytes] = []
    writing, release = threading.Event(), threading.Event()

    def write(frame: bytes) -> bool:
        writing.set()
        assert release.wait(_SOON)
        written.append(frame)
        return True

    writer = BackgroundFrameWriter(write, on_error=_never)
    writer.start()
    assert writer(b"A") is True
    assert writing.wait(_SOON)  # A is going out, and the display is stalled
    writer(b"B")  # neither of these waits for it
    writer(b"C")
    release.set()
    assert _until(lambda: written == [b"A", b"C"])  # B was replaced before its turn
    writer.stop(_SOON)


def test_a_frame_that_does_not_arrive_is_tried_again_until_it_does() -> None:
    attempts: list[bytes] = []

    def write(frame: bytes) -> bool:
        attempts.append(frame)
        return len(attempts) >= 3

    writer = BackgroundFrameWriter(write, on_error=_never, retry_every=timedelta(milliseconds=1))
    writer.start()
    writer(b"A")
    assert _until(lambda: attempts == [b"A", b"A", b"A"])
    writer.stop(_SOON)
    assert attempts == [b"A", b"A", b"A"]  # delivered on the third try, and not again


def test_a_failed_frame_gives_way_to_a_newer_one() -> None:
    attempts: list[bytes] = []
    writer: BackgroundFrameWriter[bytes]

    def write(frame: bytes) -> bool:
        attempts.append(frame)
        if frame == b"A":
            writer(b"B")  # a newer frame lands while A fails
            return False
        return True

    writer = BackgroundFrameWriter(write, on_error=_never, retry_every=timedelta(milliseconds=1))
    writer.start()
    writer(b"A")
    assert _until(lambda: attempts == [b"A", b"B"])
    writer.stop(_SOON)
    assert attempts == [b"A", b"B"]  # A wasn't tried again over B


def test_stopping_a_writer_that_never_started_is_harmless() -> None:
    writer: BackgroundFrameWriter[bytes] = BackgroundFrameWriter(lambda frame: True, on_error=_never)
    writer.stop()


def test_handing_over_reports_a_display_that_is_failing_until_a_frame_lands() -> None:
    landing = threading.Event()

    def write(frame: bytes) -> bool:
        return landing.is_set()

    writer = BackgroundFrameWriter(write, on_error=_never, retry_every=timedelta(milliseconds=1))
    writer.start()
    assert writer(b"A") is True  # nothing has failed yet
    assert _until(lambda: writer(b"A") is False)  # its write failed: the display isn't taking frames
    landing.set()
    assert _until(lambda: writer(b"A") is True)  # one landed: it is again
    writer.stop(_SOON)


def test_handing_over_reports_a_write_that_has_stalled() -> None:
    now = [0.0]
    writing, release = threading.Event(), threading.Event()

    def write(frame: bytes) -> bool:
        writing.set()
        assert release.wait(_SOON)
        return True

    writer = BackgroundFrameWriter(write, on_error=_never, stall_after=timedelta(seconds=2), monotonic=lambda: now[0])
    writer.start()
    writer(b"A")
    assert writing.wait(_SOON)
    now[0] = 1.9
    assert writer(b"B") is True  # slow, not yet stalled
    now[0] = 2.0
    assert writer(b"C") is False  # stalled: frames handed over now aren't being seen
    release.set()
    assert _until(lambda: writer(b"D") is True)
    writer.stop(_SOON)


def test_a_write_that_raises_is_reported_once_and_the_writer_keeps_going() -> None:
    outcomes: list[Exception | bool] = [RuntimeError("bug"), RuntimeError("bug"), OSError("gone"), True]
    attempts: list[bytes] = []
    errors: list[str] = []

    def write(frame: bytes) -> bool:
        attempts.append(frame)
        outcome = outcomes.pop(0) if outcomes else True
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    writer = BackgroundFrameWriter(
        write, on_error=lambda exc: errors.append(repr(exc)), retry_every=timedelta(milliseconds=1)
    )
    writer.start()
    writer(b"A")
    assert _until(lambda: len(attempts) == 4)  # tried until it landed: the thread survived each raise
    assert errors == ["RuntimeError('bug')", "OSError('gone')"]  # the same error isn't reported twice running
    writer(b"B")
    assert _until(lambda: attempts[-1] == b"B")
    writer.stop(_SOON)


# The display process.


class _Replies:
    """A display process's stdout, as the reader sees it: the lines the test has it write, then its end."""

    def __init__(self) -> None:
        self._lines: queue.SimpleQueue[bytes | Exception] = queue.SimpleQueue()
        self.drained = threading.Event()  # the reader has read to the end

    def write(self, line: bytes) -> None:
        self._lines.put(line)

    def fail(self, exc: Exception) -> None:
        self._lines.put(exc)

    def end(self) -> None:
        self._lines.put(b"")

    def readline(self) -> bytes:
        line = self._lines.get()
        if isinstance(line, Exception):
            self.drained.set()
            raise line
        if not line:
            self.drained.set()
        return line


class _BrokenPipe(io.BytesIO):
    def write(self, data: object) -> int:
        raise BrokenPipeError(32, "Broken pipe")


@dataclass
class _Process:
    """Stands in for a display process: what it was sent, the replies the test gives it, and its exit."""

    stdin: IO[bytes] | None = field(default_factory=io.BytesIO)
    replies: _Replies | None = field(default_factory=_Replies)
    exit_code: int | None = None
    waited: float | None = None

    @property
    def stdout(self) -> IO[bytes] | None:
        return cast(IO[bytes], self.replies) if self.replies is not None else None

    @property
    def sent(self) -> bytes:
        return cast(io.BytesIO, self.stdin).getvalue()

    def reply(self, line: bytes) -> None:
        assert self.replies is not None
        self.replies.write(line)

    def end(self) -> None:
        """Its output ends, and the test waits until the reader has seen everything before it."""
        assert self.replies is not None
        self.replies.end()
        assert self.replies.drained.wait(_SOON)

    def poll(self) -> int | None:
        return self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        self.waited = timeout
        return 0


class _Spawner:
    """Starts the processes (or raises the errors) it was given, in order."""

    def __init__(self, *outcomes: _Process | Exception) -> None:
        self._outcomes = list(outcomes)
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> ChildProcess:
        self.commands.append(command)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _frame(frame_id: int) -> StreamFrame:
    return StreamFrame(frame_id, 2, 1, bytes(6))


def _collect(link: DisplayLink, count: int) -> list[DisplayReceipt]:
    """The link's receipts, once there are ``count`` of them."""
    got: list[DisplayReceipt] = []

    def enough() -> bool:
        got.extend(link.receipts())
        return len(got) >= count

    assert _until(enough)
    return got


def test_a_display_process_that_fails_to_start_is_a_frame_not_written_and_is_started_again_later() -> None:
    now = [0.0]
    process = _Process()
    spawn = _Spawner(FileNotFoundError(2, "No such file or directory"), process)
    logs: list[str] = []
    link = DisplayLink(monotonic=lambda: now[0])
    sink = SinkProcess(["show", "--chain", "4"], link, spawn=spawn, monotonic=lambda: now[0], log=logs.append)
    assert sink.write(_frame(1)) is False  # not written, and nothing raised
    now[0] = 4.9
    assert sink.write(_frame(2)) is False and len(spawn.commands) == 1  # not tried again yet
    now[0] = 5.0
    assert sink.write(_frame(3)) is True
    assert spawn.commands == [["show", "--chain", "4"]] * 2
    assert process.sent == encode_legacy_frame(_frame(3))  # it hasn't said it reads ids yet
    assert logs == [
        "display: starting show --chain 4",
        "display: couldn't start it (FileNotFoundError: [Errno 2] No such file or directory); trying again shortly",
        "display: starting show --chain 4",
    ]
    process.end()


def test_a_spawn_failure_leaves_the_writer_running_and_its_next_write_starts_the_process() -> None:
    process = _Process()
    link = DisplayLink()
    logs: list[str] = []
    pipe = SinkProcess(
        ["show"], link, spawn=_Spawner(OSError("ssh: not found"), process), respawn_after=timedelta(0), log=logs.append
    )
    writer = BackgroundFrameWriter(pipe.write, on_error=_never, retry_every=timedelta(milliseconds=1))
    writer.start()
    writer(_frame(1))
    assert _until(lambda: process.sent == encode_legacy_frame(_frame(1)))
    writer(_frame(2))
    assert _until(lambda: process.sent.endswith(encode_legacy_frame(_frame(2))))  # the writer is still up
    writer.stop(_SOON)
    assert logs[1] == "display: couldn't start it (OSError: ssh: not found); trying again shortly"
    process.end()


def test_frames_carry_ids_once_the_display_process_says_it_is_ready_and_its_acks_are_receipts() -> None:
    process = _Process()
    link = DisplayLink()
    sink = SinkProcess(["show"], link, spawn=_Spawner(process), log=lambda line: None)
    assert sink.write(_frame(1))
    process.reply(READY_LINE)
    assert _until(lambda: link.acknowledges(1))
    assert sink.write(_frame(2))
    assert process.sent == encode_legacy_frame(_frame(1)) + encode_frame(_frame(2))
    process.reply(ack_line(2))
    assert _collect(link, 1) == [FrameShown(2, generation=1)]
    process.end()  # its output ended: the process is gone, and with it what it showed
    assert _collect(link, 1) == [DisplayLost(1)]


def test_acks_from_a_display_process_that_ended_are_ignored_after_a_reconnect() -> None:
    now = [0.0]
    first, second = _Process(), _Process()
    link = DisplayLink(monotonic=lambda: now[0])
    logs: list[str] = []
    sink = SinkProcess(["show"], link, spawn=_Spawner(first, second), monotonic=lambda: now[0], log=logs.append)
    sink.write(_frame(1))
    first.reply(READY_LINE)
    assert _until(lambda: link.acknowledges(1))
    sink.write(_frame(2))
    first.reply(ack_line(2))
    assert _collect(link, 1) == [FrameShown(2, generation=1)]
    sink.write(_frame(3))  # in flight when the process dies
    first.exit_code = 1
    assert sink.write(_frame(4)) is False  # it's gone: a new one comes after the respawn delay
    now[0] = 5.0
    assert sink.write(_frame(5)) is True
    first.reply(ack_line(3))  # the old process's last word arrives late
    first.end()
    second.reply(READY_LINE)
    assert _until(lambda: link.acknowledges(2))
    sink.write(_frame(6))
    second.reply(ack_line(6))
    assert _collect(link, 2) == [DisplayLost(1), FrameShown(6, generation=2)]  # never frame 3
    assert logs[1] == "display: the display process ended; restarting shortly"
    second.end()


def test_a_broken_pipe_drops_the_display_process_and_ends_its_generation() -> None:
    now = [0.0]
    process = _Process()
    link = DisplayLink(monotonic=lambda: now[0])
    logs: list[str] = []
    sink = SinkProcess(["show"], link, spawn=_Spawner(process), monotonic=lambda: now[0], log=logs.append)
    sink.write(_frame(1))
    process.reply(READY_LINE)
    process.reply(ack_line(1))
    assert _collect(link, 1) == [FrameShown(1, generation=1)]
    process.stdin = _BrokenPipe()
    assert sink.write(_frame(2)) is False
    assert link.receipts() == [DisplayLost(1)]
    assert logs[-1] == "display: lost the display process ([Errno 32] Broken pipe); restarting shortly"
    process.end()


def test_a_display_process_without_pipes_is_dropped_and_its_replies_are_not_read() -> None:
    process = _Process(stdin=None, replies=None)
    link = DisplayLink()
    sink = SinkProcess(["show"], link, spawn=_Spawner(process), log=lambda line: None)
    assert sink.write(_frame(1)) is False


def test_a_reader_whose_pipe_breaks_ends_its_generation() -> None:
    process = _Process()
    link = DisplayLink()
    sink = SinkProcess(["show"], link, spawn=_Spawner(process), log=lambda line: None)
    sink.write(_frame(1))
    process.reply(READY_LINE)
    process.reply(ack_line(1))
    assert process.replies is not None
    process.replies.fail(ValueError("I/O operation on closed file"))
    assert _collect(link, 2) == [FrameShown(1, generation=1), DisplayLost(1)]


def test_closing_ends_the_display_process_input_and_waits_for_it() -> None:
    SinkProcess(["show"], DisplayLink(), spawn=_Spawner(), log=lambda line: None).close()  # never started
    process = _Process()
    sink = SinkProcess(["show"], DisplayLink(), spawn=_Spawner(process), log=lambda line: None)
    sink.write(_frame(1))
    stdin = process.stdin
    assert stdin is not None
    sink.close()
    assert stdin.closed and process.waited == 10
    process.end()
    bare = _Process()
    sink = SinkProcess(["show"], DisplayLink(), spawn=_Spawner(bare), log=lambda line: None)
    sink.write(_frame(1))
    bare.stdin = None  # its input already gone
    sink.close()
    assert bare.waited == 10
    bare.end()


def test_a_heartbeat_asked_for_goes_out_on_the_writer_s_thread_unless_a_frame_is_waiting() -> None:
    beats: list[int] = []
    landed = threading.Event()
    frames: list[int] = []

    def heartbeat() -> bool:
        beats.append(len(frames))
        landed.set()
        return True

    def write(frame: int) -> bool:
        frames.append(frame)
        return True

    writer = BackgroundFrameWriter(write, on_error=_never, heartbeat=heartbeat)
    writer.start()
    writer.beat()
    assert landed.wait(_SOON) and beats == [0]
    writer(1)
    assert _until(lambda: frames == [1])
    writer.stop(_SOON)
    quiet = BackgroundFrameWriter(write, on_error=_never)  # nothing to beat with: asking does nothing
    quiet.beat()
    waiting = BackgroundFrameWriter(write, on_error=_never, heartbeat=heartbeat)
    waiting(2)
    waiting.beat()  # a frame is waiting to go out, which says as much
    waiting.start()
    assert _until(lambda: frames == [1, 2])
    waiting.stop(_SOON)
    assert beats == [0]


def test_a_heartbeat_that_fails_is_reported_and_the_writer_keeps_going() -> None:
    errors: list[str] = []
    frames: list[int] = []

    def heartbeat() -> bool:
        raise BrokenPipeError("gone")

    def write(frame: int) -> bool:
        frames.append(frame)
        return True

    writer = BackgroundFrameWriter(write, on_error=lambda exc: errors.append(repr(exc)), heartbeat=heartbeat)
    writer.start()
    writer.beat()
    assert _until(lambda: errors == ["BrokenPipeError('gone')"])
    writer(1)
    assert _until(lambda: frames == [1])
    writer.stop(_SOON)


def _announce(process: _Process, link: DisplayLink, boot: str = "boot-a") -> None:
    process.reply(READY_LINE)
    process.reply(b"OMN3 READY %s\n" % boot.encode())
    assert _until(lambda: link.protocol(1)[0] is LinkProtocol.SESSIONS)


def test_a_receiver_that_reads_sessions_gets_its_session_s_frames_and_heartbeats_and_its_replies_are_heard() -> None:
    now = [0.0]
    process = _Process()
    link = DisplayLink(monotonic=lambda: now[0])
    sink = SinkProcess(["show"], link, spawn=_Spawner(process), monotonic=lambda: now[0], log=lambda line: None)
    assert sink.heartbeat() is True  # no display process yet: nothing to tell
    assert sink.write(_frame(1))
    assert sink.heartbeat() is True and process.sent == encode_legacy_frame(_frame(1))  # it hasn't said what it reads
    _announce(process, link)
    session = link.protocol(1)[1]
    assert sink.write(_frame(2)) and sink.heartbeat()
    assert process.sent == encode_legacy_frame(_frame(1)) + encode_session_frame(
        session, 2, 2, 1, bytes(6)
    ) + encode_heartbeat(session)
    process.reply(b"SHOWN %d 2\n" % session)
    assert _collect(link, 1) == [FrameShown(2, generation=1)]
    now[0] = 7.0
    process.reply(b"ALIVE %d\n" % session)
    process.reply(b"BLANK %d\n" % session)
    receipts = _collect(link, 1)
    assert receipts == [DisplayLost(1)] and isinstance(receipts[0], DisplayLost)
    assert receipts[0].cause is LossCause.BLANKED
    assert sink.write(_frame(3))
    assert process.sent.endswith(encode_session_frame(session + 1, 3, 2, 1, bytes(6)))  # a new session
    process.end()


def test_an_older_receiver_gets_no_heartbeat_it_would_read_as_a_broken_frame() -> None:
    process = _Process()
    link = DisplayLink()
    sink = SinkProcess(["show"], link, spawn=_Spawner(process), log=lambda line: None)
    sink.write(_frame(1))
    process.reply(READY_LINE)
    assert _until(lambda: link.acknowledges(1))
    sent = process.sent
    assert sink.heartbeat() is True and process.sent == sent
    process.exit_code = 0
    assert sink.heartbeat() is True  # the process is gone: the next frame starts a new one
    process.end()


def test_a_heartbeat_into_a_broken_pipe_drops_the_display_process() -> None:
    process = _Process()
    link = DisplayLink()
    logs: list[str] = []
    sink = SinkProcess(["show"], link, spawn=_Spawner(process), log=logs.append)
    sink.write(_frame(1))
    _announce(process, link)
    process.stdin = _BrokenPipe()
    assert sink.heartbeat() is False
    assert logs[-1] == "display: lost the display process ([Errno 32] Broken pipe); restarting shortly"
    process.end()
