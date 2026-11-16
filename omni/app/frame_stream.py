"""Frames as raw RGB bytes down a pipe, to a display process on the other end, and its word on what it showed.

The LED matrix library needs root and the board's system Python, while the app needs neither. So the app
can draw into memory and hand each finished frame to a small display process (`tools/board_frame_sink.py`)
that owns the panel. The two can share a board, or the display process can sit across an SSH session.

The app writes frames to the display process's stdin. Each frame is the 4-byte magic ``OMN2``, the frame's
id as a little-endian 32-bit integer, the width and height as little-endian 16-bit integers, then the
pixels row by row, three bytes each. An older display process reads only ``OMNF`` frames: the same, with
no id.

The display process answers on its stdout, a line at a time:
- ``OMN2 READY`` once it's up and reads ``OMN2`` frames. Until it says so, it gets ``OMNF`` frames.
- ``ACK <id>`` once the frame with that id is on the panel.
Anything else it prints is ignored.

A frame counts as shown only once the display process says so. Each display process is a *generation*,
whose panel starts blank: a `DisplayLink` gathers the word from the current one, and drops word from one
that has ended, so a frame in flight when its process died is never counted. A frame identical to the
last one, which the display shows already, isn't sent again. A display process that never says it's ready
(one older than the acknowledgements) still works: once frames have gone to it for a few seconds with no
word back, each frame written to it counts as shown, unconfirmed.

Writing to a pipe can block: across SSH, a stalled link stops the other end reading. A
`BackgroundFrameWriter` takes the writing off the frame loop, so drawing goes on while a frame waits to
go out. `SinkProcess` runs the display process: it starts it, writes the frames, reads the replies on a
thread of its own, and starts a new one a few seconds after one drops.
"""

from __future__ import annotations

import dataclasses
import shlex
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import IO, Generic, Protocol, TypeVar, runtime_checkable

from omni.core.enum import PanelProfile
from omni.panels.geometry import geometry_for
from omni.renderers.canvas import Canvas
from omni.renderers.pillow_canvas import PillowCanvas

__all__ = [
    "FRAME_MAGIC",
    "LEGACY_FRAME_MAGIC",
    "READY_LINE",
    "RESPAWN_AFTER",
    "UNCONFIRMED_AFTER",
    "AcknowledgingSink",
    "BackgroundFrameWriter",
    "ChildProcess",
    "DisplayLink",
    "DisplayLost",
    "DisplayReceipt",
    "DisplayUnavailable",
    "FrameShown",
    "SinkProcess",
    "StreamDisplaySink",
    "StreamFrame",
    "ack_line",
    "encode_frame",
    "encode_legacy_frame",
    "read_replies",
]

FRAME_MAGIC = b"OMN2"
LEGACY_FRAME_MAGIC = b"OMNF"
READY_LINE = b"OMN2 READY\n"
RESPAWN_AFTER = timedelta(seconds=5)  # before starting a display process again, after one dropped
UNCONFIRMED_AFTER = timedelta(seconds=5)  # of frames going to a display process that never answers
_LAST_ID = 2**32 - 1  # ids run 1 to this on the wire, then start over (13 years at 10 frames a second)

FrameT = TypeVar("FrameT")


class DisplayUnavailable(OSError):
    """The display isn't taking frames: the frame just committed won't be seen."""


@dataclass(frozen=True, slots=True)
class StreamFrame:
    """One drawn frame for the display process: its id, its size, and its pixels, three bytes each."""

    id: int
    width: int
    height: int
    pixels: bytes

    def __post_init__(self) -> None:
        if len(self.pixels) != self.width * self.height * 3:
            raise ValueError("a frame is three bytes per pixel")
        if not 0 < self.id <= _LAST_ID:
            raise ValueError("a frame's id is a 32-bit number above zero")


def encode_frame(frame: StreamFrame) -> bytes:
    """One frame on the wire, with its id, for a display process that acknowledges frames."""
    return FRAME_MAGIC + struct.pack("<IHH", frame.id, frame.width, frame.height) + frame.pixels


def encode_legacy_frame(frame: StreamFrame) -> bytes:
    """One frame on the wire without its id, for a display process that hasn't said it reads ids."""
    return LEGACY_FRAME_MAGIC + struct.pack("<HH", frame.width, frame.height) + frame.pixels


def ack_line(frame_id: int) -> bytes:
    """What the display process writes once the frame with ``frame_id`` is on the panel."""
    return b"ACK %d\n" % frame_id


class _Lines(Protocol):
    def readline(self) -> bytes: ...


def read_replies(stream: _Lines, *, ready: Callable[[], None], shown: Callable[[int], None]) -> None:
    """Read a display process's replies until its output ends: that it's ready, then each frame it shows."""
    for line in iter(stream.readline, b""):
        words = line.split()
        if words == READY_LINE.split():
            ready()
        elif len(words) == 2 and words[0] == b"ACK" and words[1].isdigit():
            shown(int(words[1]))


@dataclass(frozen=True, slots=True)
class FrameShown:
    """The display showed a frame: which one (its id), on which display process, and whether it said so.

    Unconfirmed means only that the frame was written to a display process that never answers.
    """

    frame: int
    generation: int
    confirmed: bool = True


@dataclass(frozen=True, slots=True)
class DisplayLost:
    """A display process that had shown frames ended: its panel no longer shows them."""

    generation: int


DisplayReceipt = FrameShown | DisplayLost


@runtime_checkable
class AcknowledgingSink(Protocol):
    """A display that says which frames it actually showed, after the fact."""

    @property
    def profile(self) -> PanelProfile: ...

    def new_frame(self) -> Canvas:
        """A fresh canvas of this display's profile to draw the next frame into."""
        ...

    def send(self, frame: Canvas) -> int:
        """Hand a drawn frame to the display; returns the id its `FrameShown` will carry."""
        ...

    def receipts(self) -> list[DisplayReceipt]:
        """What the display showed, or lost, since the last ask, oldest first."""
        ...


class DisplayLink:
    """The word from the display processes, gathered across threads: which frames the current one showed.

    The writer says when a display process `started` and `ended`, and each frame `written` to it. The
    reader of its replies says when it's `ready` (it acknowledges frames) and each frame it `shown`. Word
    from any but the current generation is dropped. The end of a generation that had shown frames is a
    receipt of its own, `DisplayLost`: its panel is blank now.

    A generation that never says it's ready gets no acknowledgements to wait for. Once frames have been
    written to it for `unconfirmed_after`, each one written from then on counts as shown, unconfirmed.
    """

    def __init__(
        self,
        *,
        unconfirmed_after: timedelta = UNCONFIRMED_AFTER,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._unconfirmed_after = unconfirmed_after.total_seconds()
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._current: int | None = None  # the generation running now
        self._acknowledges = False  # the current generation said it's ready
        self._first_written: float | None = None  # when a frame first went to the current generation
        self._showing: FrameShown | None = None  # the newest frame the current generation showed
        self._receipts: list[DisplayReceipt] = []

    def started(self, generation: int) -> None:
        with self._lock:
            self._end()
            self._current, self._acknowledges, self._first_written = generation, False, None

    def ended(self, generation: int) -> None:
        with self._lock:
            if generation == self._current:
                self._end()

    def ready(self, generation: int) -> None:
        with self._lock:
            if generation == self._current:
                self._acknowledges = True

    def acknowledges(self, generation: int) -> bool:
        """Whether ``generation`` is current and acknowledges frames (so its frames carry ids)."""
        with self._lock:
            return generation == self._current and self._acknowledges

    def written(self, generation: int, frame: int) -> None:
        with self._lock:
            if generation != self._current or self._acknowledges:
                return
            now = self._monotonic()
            if self._first_written is None:
                self._first_written = now
            if now - self._first_written >= self._unconfirmed_after:
                self._show(FrameShown(frame, generation, confirmed=False))

    def shown(self, generation: int, frame: int) -> None:
        with self._lock:
            if generation == self._current:
                self._show(FrameShown(frame, generation))

    def vouch(self, frame: int, like: int) -> bool:
        """``frame`` has the same pixels as ``like``, the frame handed over last. If the display shows
        ``like``, it shows ``frame`` too: a receipt for it now, with no need to send it."""
        with self._lock:
            showing = self._showing
            if showing is None or showing.frame != like:
                return False
            self._show(dataclasses.replace(showing, frame=frame))
            return True

    def receipts(self) -> list[DisplayReceipt]:
        with self._lock:
            receipts, self._receipts = self._receipts, []
            return receipts

    def _show(self, receipt: FrameShown) -> None:
        self._showing = receipt
        self._receipts.append(receipt)

    def _end(self) -> None:
        if self._showing is not None:
            self._receipts.append(DisplayLost(self._showing.generation))
        self._current, self._showing = None, None


class StreamDisplaySink:
    """A display sink that draws in memory and hands each frame to a writer for the display process.

    Its `DisplayLink` says which frames the display showed (`receipts`). A frame with the same pixels as
    the one handed over last, which the display shows already, isn't handed over again: its receipt comes
    at once. Anything else goes to the writer, and is shown only once the display says so.
    """

    def __init__(self, profile: PanelProfile, write: Callable[[StreamFrame], bool], link: DisplayLink) -> None:
        self._profile = profile
        self._write = write
        self._link = link
        self._last_id = 0  # the frame handed over last
        self._last_pixels: bytes | None = None

    @property
    def profile(self) -> PanelProfile:
        return self._profile

    def new_frame(self) -> Canvas:
        geometry = geometry_for(self._profile)
        return PillowCanvas(geometry.width, geometry.height)

    def send(self, frame: Canvas) -> int:
        """Hand ``frame`` over; raises `DisplayUnavailable` when the writer says frames aren't going out."""
        if not isinstance(frame, PillowCanvas):
            raise TypeError("StreamDisplaySink expects a PillowCanvas from new_frame()")
        pixels = frame.image().tobytes()
        last, frame_id = self._last_id, self._last_id % _LAST_ID + 1
        self._last_id = frame_id
        if pixels == self._last_pixels and self._link.vouch(frame_id, like=last):
            return frame_id
        self._last_pixels = pixels
        if not self._write(StreamFrame(frame_id, frame.width, frame.height, pixels)):
            raise DisplayUnavailable("the display isn't taking frames")
        return frame_id

    def commit(self, frame: Canvas) -> None:
        self.send(frame)

    def receipts(self) -> list[DisplayReceipt]:
        return self._link.receipts()


class BackgroundFrameWriter(Generic[FrameT]):
    """A frame writer that writes on its own thread, newest frame first.

    Only the newest frame waits: one handed over while another is going out replaces any still waiting.
    A frame that doesn't arrive is tried again until it does, or until a newer one replaces it, so the
    display ends up on the newest frame either way. A write that raises is a frame that didn't arrive:
    the error goes to ``on_error`` (once, until a write lands or a different error comes), and the thread
    goes on. Handing a frame over never blocks. It reports the frame as going out unless the writer is
    failing (its last write didn't arrive) or stalled (a write has been going out for `stall_after` or
    longer), so the caller knows when the display isn't taking frames.
    """

    def __init__(
        self,
        write: Callable[[FrameT], bool],
        *,
        on_error: Callable[[Exception], None],
        retry_every: timedelta = timedelta(milliseconds=500),
        stall_after: timedelta = timedelta(seconds=2),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._write = write
        self._on_error = on_error
        self._retry_every = retry_every.total_seconds()
        self._stall_after = stall_after.total_seconds()
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._failing = False  # the last write didn't arrive, and none has since
        self._error: str | None = None  # the last error reported, until a write lands
        self._writing_since: float | None = None  # when the write going out now began
        self._waiting: FrameT | None = None
        self._ready = threading.Event()  # a frame is waiting, or the writer should stop
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="frame-writer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stop after the frame going out now, if any; a frame still waiting is dropped."""
        self._stopped.set()
        self._ready.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def __call__(self, frame: FrameT) -> bool:
        with self._lock:
            self._waiting = frame
            self._ready.set()
            since = self._writing_since
            stalled = since is not None and self._monotonic() - since >= self._stall_after
            return not (self._failing or stalled)

    def _run(self) -> None:
        # `_ready` is set only alongside a waiting frame, both under the lock, or by `stop`.
        while True:
            self._ready.wait()
            if self._stopped.is_set():
                return
            with self._lock:
                frame, self._waiting = self._waiting, None
                self._ready.clear()
                self._writing_since = self._monotonic()
            assert frame is not None
            delivered = self._attempt(frame)
            with self._lock:
                self._writing_since, self._failing = None, not delivered
            if delivered:
                continue
            with self._lock:
                if self._waiting is None:
                    self._waiting = frame  # try it again, unless a newer one came meanwhile
                    self._ready.set()
            self._stopped.wait(self._retry_every)

    def _attempt(self, frame: FrameT) -> bool:
        try:
            delivered = self._write(frame)
        except Exception as exc:  # a broken write never stops the writer: the frame just didn't arrive
            if repr(exc) != self._error:
                self._error = repr(exc)
                self._on_error(exc)
            return False
        if delivered:
            self._error = None
        return delivered


class ChildProcess(Protocol):
    """The slice of `subprocess.Popen` a `SinkProcess` uses."""

    @property
    def stdin(self) -> IO[bytes] | None: ...

    @property
    def stdout(self) -> IO[bytes] | None: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def _spawn(command: list[str]) -> ChildProcess:  # pragma: no cover - starts a real process
    return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)


@dataclass(frozen=True, slots=True)
class _Child:
    process: ChildProcess
    generation: int


class SinkProcess:
    """A display process fed frames on its stdin, its replies read on a thread of its own.

    `write` is the writer for a `BackgroundFrameWriter`. It starts a display process when none is running,
    and a new one `respawn_after` one dropped or failed to start. A frame that doesn't go out (no process
    yet, a failed start, a broken pipe) is reported as not arrived, never raised. Each process it starts
    is a new generation in its `DisplayLink`, and gets frames with ids once it says it's ready.
    """

    def __init__(
        self,
        command: Sequence[str],
        link: DisplayLink,
        *,
        spawn: Callable[[list[str]], ChildProcess] = _spawn,
        respawn_after: timedelta = RESPAWN_AFTER,
        monotonic: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
    ) -> None:
        self._command = list(command)
        self._link = link
        self._spawn = spawn
        self._respawn_after = respawn_after.total_seconds()
        self._monotonic = monotonic
        self._log = log
        self._child: _Child | None = None
        self._retry_at: float | None = None  # no new process before this
        self._started = 0  # display processes started so far

    def write(self, frame: StreamFrame) -> bool:
        child = self._child
        if child is not None and child.process.poll() is not None:
            self._drop(child, "the display process ended")
            child = None
        if child is None:
            child = self._start()
            if child is None:
                return False
        acknowledges = self._link.acknowledges(child.generation)
        try:
            stdin = child.process.stdin
            if stdin is None:
                raise BrokenPipeError("the display process has no stdin")
            stdin.write(encode_frame(frame) if acknowledges else encode_legacy_frame(frame))
            stdin.flush()
        except OSError as exc:
            self._drop(child, f"lost the display process ({exc})")
            return False
        self._link.written(child.generation, frame.id)
        return True

    def close(self) -> None:
        """Close the display process's stdin, and wait for it to finish."""
        child = self._child
        if child is None:
            return
        if child.process.stdin is not None:
            child.process.stdin.close()
        child.process.wait(timeout=10)

    def _start(self) -> _Child | None:
        if self._retry_at is not None and self._monotonic() < self._retry_at:
            return None
        self._log(f"display: starting {shlex.join(self._command)}")
        try:
            process = self._spawn(self._command)
        except Exception as exc:  # a failed start is a frame that didn't go out: try again later
            self._log(f"display: couldn't start it ({type(exc).__name__}: {exc}); trying again shortly")
            self._retry_at = self._monotonic() + self._respawn_after
            return None
        self._started += 1
        child = _Child(process, self._started)
        self._link.started(child.generation)
        self._child = child
        stdout = process.stdout
        if stdout is not None:
            name = f"display-replies-{child.generation}"
            threading.Thread(target=self._read, args=(stdout, child.generation), name=name, daemon=True).start()
        return child

    def _read(self, stdout: IO[bytes], generation: int) -> None:
        """The replies of one display process, until its output ends: then that generation has ended."""
        try:
            read_replies(
                stdout,
                ready=lambda: self._link.ready(generation),
                shown=lambda frame: self._link.shown(generation, frame),
            )
        except (OSError, ValueError):  # its output closed under the reader
            pass
        finally:
            self._link.ended(generation)

    def _drop(self, child: _Child, why: str) -> None:
        self._log(f"display: {why}; restarting shortly")
        self._link.ended(child.generation)
        self._child = None
        self._retry_at = self._monotonic() + self._respawn_after
