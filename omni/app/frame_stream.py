"""Frames as raw RGB bytes down a pipe, to a display process on the other end, and its word on what it showed.

The LED matrix library needs root and the board's system Python, while the app needs neither. So the app
can draw into memory and hand each finished frame to a small display process, the receiver
(`tools/board_frame_sink.py`), that owns the panel. The two can share a board, or the receiver can sit across an SSH
session. The conversation itself is `omni.app.receiver`: the wire, the replies and the receiver's watchdog.

The app writes to the receiver's stdin, and reads its replies from its stdout, a line at a time. What it writes
depends on what the receiver said it reads:
- a receiver that says ``OMN3 READY <boot>`` gets ``OMN3`` frames, each with its id and the *output session* it
  belongs to, and a heartbeat (``OMNH``) at least every `HEARTBEAT_EVERY` while the app runs, even while the frame
  is unchanged. It answers ``SHOWN <session> <id>`` for each frame on the panel, ``ALIVE <session>`` for each
  heartbeat, and ``BLANK <session>`` when its watchdog blanked the panel (`WATCHDOG` without a frame or a heartbeat);
- one that says only ``OMN2 READY`` (older, without sessions) gets ``OMN2`` frames, with ids, and answers ``ACK <id>``;
- one that says neither (older still) gets ``OMNF`` frames, with no id.

A frame counts as shown only once the receiver says so. Each display process is a *generation*, whose panel starts
blank, and each generation runs one *output session* at a time. A `DisplayLink` gathers the word from the current
session and drops word from any other, so a frame in flight when its session ended is never counted, and a late
acknowledgement never renews a session that has ended. A session ends when its process ends (a dropped transport),
when the receiver announces a new boot id (it restarted), when it says its watchdog blanked the panel, or when a
receiver that answers heartbeats has said nothing for `SILENT_AFTER`: in each case what it showed is lost
(`DisplayLost`), and the frames after go to a new session. A frame identical to the last one, which the display
shows already, isn't sent again. A receiver that never says it's ready still works: once frames have gone to it for
a few seconds with no word back, each frame written to it counts as shown, unconfirmed.

Writing to a pipe can block: across SSH, a stalled link stops the other end reading. A
`BackgroundFrameWriter` takes the writing off the frame loop, so drawing goes on while a frame waits to
go out. `SinkProcess` runs the display process: it starts it, writes the frames and heartbeats, reads the replies on
a thread of its own, and starts a new one a few seconds after one drops. Heartbeats are asked for by the frame loop
itself (`StreamDisplaySink`), so an app whose frame loop stops sends none, and the receiver blanks the panel.
"""

from __future__ import annotations

import dataclasses
import shlex
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import IO, Generic, Protocol, TypeVar, runtime_checkable

from omni.app.receiver import (
    FRAME_MAGIC,
    HEARTBEAT_EVERY,
    LEGACY_FRAME_MAGIC,
    READY_LINE,
    WATCHDOG,
    Reply,
    ReplyKind,
    encode_heartbeat,
    encode_session_frame,
    parse_reply,
)
from omni.core.clock import MonotonicTime
from omni.core.enum import PanelProfile, StrEnumMixin
from omni.panels.geometry import geometry_for
from omni.renderers.canvas import Canvas
from omni.renderers.pillow_canvas import PillowCanvas

__all__ = [
    "FRAME_MAGIC",
    "HEARTBEAT_EVERY",
    "LEGACY_FRAME_MAGIC",
    "READY_LINE",
    "RESPAWN_AFTER",
    "SILENT_AFTER",
    "UNCONFIRMED_AFTER",
    "WATCHDOG",
    "AcknowledgingSink",
    "BackgroundFrameWriter",
    "ChildProcess",
    "DisplayLink",
    "DisplayLost",
    "DisplayReceipt",
    "DisplayUnavailable",
    "FrameShown",
    "LinkProtocol",
    "LossCause",
    "SinkProcess",
    "StreamDisplay",
    "StreamDisplaySink",
    "StreamFrame",
    "ack_line",
    "encode_frame",
    "encode_legacy_frame",
    "read_replies",
    "stream_display",
]

RESPAWN_AFTER = timedelta(seconds=5)  # before starting a display process again, after one dropped
UNCONFIRMED_AFTER = timedelta(seconds=5)  # of frames going to a display process that never answers
SILENT_AFTER = timedelta(seconds=WATCHDOG)  # a receiver that answers heartbeats, silent this long: its panel is lost
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


def read_replies(
    stream: _Lines,
    *,
    ready: Callable[[], None],
    shown: Callable[[int], None],
    heard: Callable[[Reply], None] = lambda reply: None,
) -> None:
    """Read a display process's replies until its output ends: that it's ready (``OMN2``) and each frame it shows
    (``ACK``), and anything a receiver with sessions says (``heard``)."""
    for line in iter(stream.readline, b""):
        reply = parse_reply(line)
        if reply is None:
            continue
        if reply.kind is ReplyKind.READY_V2:
            ready()
        elif reply.kind is ReplyKind.ACK:
            assert reply.frame is not None  # an ACK names its frame
            shown(reply.frame)
        else:
            heard(reply)


class LossCause(StrEnumMixin, str, Enum):
    """Why a display's panel no longer shows what it did."""

    ENDED = "the display process ended"  # or its transport dropped
    REBOOTED = "the receiver restarted"  # it announced a new boot id
    BLANKED = "the receiver blanked the panel"  # its watchdog: no frame or heartbeat for too long
    SILENT = "the receiver went silent"  # it answers heartbeats, and hasn't for too long


@dataclass(frozen=True, slots=True)
class FrameShown:
    """The display showed a frame: which one (its id), on which display process, and whether it said so.

    Unconfirmed means only that the frame was written to a display process that never answers. ``session`` is the
    output session it was shown in, and ``heard`` when the app heard so, on the link's monotonic clock: neither
    makes two receipts of the same frame different.
    """

    frame: int
    generation: int
    confirmed: bool = True
    session: int = field(default=0, compare=False)
    heard: MonotonicTime | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class DisplayLost:
    """A display process that had shown frames ended, or its session did: its panel no longer shows them."""

    generation: int
    session: int = field(default=0, compare=False)
    cause: LossCause = field(default=LossCause.ENDED, compare=False)
    heard: MonotonicTime | None = field(default=None, compare=False)
    since: MonotonicTime | None = field(default=None, compare=False)  # its display's last word: shown until then


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


class LinkProtocol(StrEnumMixin, str, Enum):
    """What a display process said it reads, and so what it gets."""

    UNCONFIRMED = "unconfirmed"  # it never said: frames without ids, credited unconfirmed
    ACKNOWLEDGING = "acknowledging"  # ``OMN2``: frames with ids, each acknowledged
    SESSIONS = "sessions"  # ``OMN3``: frames in a session, heartbeats, and its watchdog


class DisplayLink:
    """The word from the display processes, gathered across threads: which frames the current session showed.

    The writer says when a display process `started` and `ended`, and each frame `written` to it. The reader of its
    replies says when it's `ready` (it acknowledges frames), when it `announced` its boot id (it reads sessions),
    each frame it `shown`, each heartbeat it answered (`alive`), and when its watchdog `blanked` the panel. Word from
    any but the current generation and session is dropped. The end of a session that had shown frames is a receipt
    of its own, `DisplayLost`: its panel is blank now, or no longer known. A session ends with its process, at a new
    boot id, at the receiver's blank, and once a receiver that answers heartbeats has been silent `silent_after`;
    the next frames go to a new one.

    A generation that never says it's ready gets no acknowledgements to wait for. Once frames have been written to
    it for `unconfirmed_after`, each one written from then on counts as shown, unconfirmed.
    """

    def __init__(
        self,
        *,
        unconfirmed_after: timedelta = UNCONFIRMED_AFTER,
        silent_after: timedelta = SILENT_AFTER,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._unconfirmed_after = unconfirmed_after.total_seconds()
        self._silent_after = silent_after.total_seconds()
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._current: int | None = None  # the generation running now
        self._protocol = LinkProtocol.UNCONFIRMED  # what the current generation said it reads
        self._boot: str | None = None  # the current generation's boot id, once announced
        self._session = 0  # the output session running now: each new one is numbered higher
        self._heard: float | None = None  # when the current session last heard from a receiver with sessions
        self._word: float | None = None  # when the current session's display last said it showed its frame
        self._first_written: float | None = None  # when a frame first went to the current generation
        self._showing: FrameShown | None = None  # the newest frame the current session showed
        self._receipts: list[DisplayReceipt] = []

    def started(self, generation: int) -> None:
        with self._lock:
            self._end(LossCause.ENDED)
            self._current, self._protocol, self._boot, self._first_written = (
                generation,
                LinkProtocol.UNCONFIRMED,
                None,
                None,
            )
            self._session += 1

    def ended(self, generation: int) -> None:
        with self._lock:
            if generation == self._current:
                self._end(LossCause.ENDED)

    def ready(self, generation: int) -> None:
        with self._lock:
            if generation == self._current and self._protocol is LinkProtocol.UNCONFIRMED:
                self._protocol = LinkProtocol.ACKNOWLEDGING

    def announced(self, generation: int, boot: str) -> None:
        """The receiver of ``generation`` reads sessions, and its run is ``boot``: a new one ends the session."""
        with self._lock:
            if generation != self._current:
                return
            if self._boot is not None and boot != self._boot:
                self._lose(LossCause.REBOOTED)
            self._protocol, self._boot, self._heard = LinkProtocol.SESSIONS, boot, self._monotonic()

    def acknowledges(self, generation: int) -> bool:
        """Whether ``generation`` is current and acknowledges frames (so its frames carry ids)."""
        with self._lock:
            return generation == self._current and self._protocol is not LinkProtocol.UNCONFIRMED

    def protocol(self, generation: int) -> tuple[LinkProtocol, int]:
        """What ``generation`` reads, and the session its frames and heartbeats go to now. A generation no longer
        current reads nothing more: unconfirmed."""
        with self._lock:
            if generation != self._current:
                return LinkProtocol.UNCONFIRMED, self._session
            return self._protocol, self._session

    def written(self, generation: int, frame: int) -> None:
        with self._lock:
            if generation != self._current or self._protocol is not LinkProtocol.UNCONFIRMED:
                return
            now = self._monotonic()
            if self._first_written is None:
                self._first_written = now
            if now - self._first_written >= self._unconfirmed_after:
                self._show(FrameShown(frame, generation, confirmed=False, session=self._session))

    def shown(self, generation: int, frame: int, *, session: int | None = None) -> None:
        """The receiver of ``generation`` showed ``frame``: in ``session``, from one with sessions."""
        with self._lock:
            if generation != self._current:
                return
            if session is not None:
                if self._protocol is not LinkProtocol.SESSIONS or session != self._session:
                    return  # a session that has ended: its late word renews nothing
                self._heard = self._monotonic()
            self._show(FrameShown(frame, generation, session=self._session))

    def alive(self, generation: int, session: int) -> None:
        """The receiver of ``generation`` answered a heartbeat of ``session``."""
        with self._lock:
            if generation == self._current and session == self._session:
                self._heard = self._word = self._monotonic()

    def blanked(self, generation: int, session: int) -> None:
        """The receiver of ``generation`` blanked its panel in ``session``: the session is over."""
        with self._lock:
            if generation == self._current and session == self._session:
                self._lose(LossCause.BLANKED)

    def vouch(self, frame: int, like: int) -> bool:
        """``frame`` has the same pixels as ``like``, the frame handed over last. If the display shows
        ``like``, it shows ``frame`` too: a receipt for it now, with no need to send it."""
        with self._lock:
            self._check_silence()
            showing = self._showing
            if showing is None or showing.frame != like:
                return False
            self._show(dataclasses.replace(showing, frame=frame), word=False)
            return True

    def receipts(self) -> list[DisplayReceipt]:
        with self._lock:
            self._check_silence()
            receipts, self._receipts = self._receipts, []
            return receipts

    def _show(self, receipt: FrameShown, *, word: bool = True) -> None:
        """A receipt, heard now; ``word`` when the display said so itself, rather than its frame being vouched for."""
        now = self._monotonic()
        receipt = dataclasses.replace(receipt, heard=MonotonicTime(now))
        self._showing = receipt
        self._receipts.append(receipt)
        if word:
            self._word = now

    def _check_silence(self) -> None:
        """A receiver that answers heartbeats and has been silent too long has lost its panel, as far as anyone
        can tell: its watchdog has blanked it, or the word back is gone."""
        heard = self._heard
        if self._protocol is LinkProtocol.SESSIONS and heard is not None:
            if self._monotonic() - heard >= self._silent_after:
                self._lose(LossCause.SILENT)

    def _lose(self, cause: LossCause) -> None:
        """The current session is over: what it showed is lost, and the next frames go to a new one."""
        showing = self._showing
        if showing is not None:
            self._receipts.append(self._lost(showing, cause))
        self._showing, self._word = None, None
        self._session += 1
        self._heard = self._monotonic() if self._heard is not None else None

    def _end(self, cause: LossCause) -> None:
        if self._showing is not None:
            self._receipts.append(self._lost(self._showing, cause))
        self._current, self._showing, self._heard, self._word = None, None, None, None

    def _lost(self, showing: FrameShown, cause: LossCause) -> DisplayLost:
        """The loss of what ``showing`` put up, heard now: shown, as far as anyone knows, until the display's last
        word (or, for a frame credited on no word, until it was)."""
        word = self._word if self._word is not None else showing.heard.seconds if showing.heard is not None else None
        since = MonotonicTime(word) if word is not None else None
        heard = MonotonicTime(self._monotonic())
        return DisplayLost(showing.generation, session=self._session, cause=cause, heard=heard, since=since)


class StreamDisplaySink:
    """A display sink that draws in memory and hands each frame to a writer for the display process.

    Its `DisplayLink` says which frames the display showed (`receipts`). A frame with the same pixels as
    the one handed over last, which the display shows already, isn't handed over again: its receipt comes
    at once. Anything else goes to the writer, and is shown only once the display says so.

    Given ``beat``, it asks for a heartbeat whenever `heartbeat_every` has passed since it last handed over a frame
    or asked for one, so a receiver with a watchdog keeps an unchanged frame up for as long as frames are drawn.
    """

    def __init__(
        self,
        profile: PanelProfile,
        write: Callable[[StreamFrame], bool],
        link: DisplayLink,
        *,
        beat: Callable[[], None] | None = None,
        heartbeat_every: timedelta = timedelta(seconds=HEARTBEAT_EVERY),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profile = profile
        self._write = write
        self._link = link
        self._beat = beat
        self._heartbeat_every = heartbeat_every.total_seconds()
        self._monotonic = monotonic
        self._last_id = 0  # the frame handed over last
        self._last_pixels: bytes | None = None
        self._last_out: float | None = None  # when a frame or a heartbeat was last handed over

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
        now = self._monotonic()
        if pixels == self._last_pixels and self._link.vouch(frame_id, like=last):
            if self._beat is not None and (self._last_out is None or now - self._last_out >= self._heartbeat_every):
                self._last_out = now
                self._beat()
            return frame_id
        self._last_pixels = pixels
        self._last_out = now  # a frame says as much as a heartbeat
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

    Given ``heartbeat``, `beat` asks for a heartbeat to go out on the same thread: once, unless a frame is
    waiting, which says as much. A heartbeat that fails is just one missed: the next is asked for soon enough.
    """

    def __init__(
        self,
        write: Callable[[FrameT], bool],
        *,
        on_error: Callable[[Exception], None],
        heartbeat: Callable[[], bool] | None = None,
        retry_every: timedelta = timedelta(milliseconds=500),
        stall_after: timedelta = timedelta(seconds=2),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._write = write
        self._heartbeat = heartbeat
        self._beat = False  # a heartbeat is asked for
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

    def beat(self) -> None:
        """Ask for a heartbeat to go out, unless a frame is waiting to."""
        with self._lock:
            if self._heartbeat is not None and self._waiting is None:
                self._beat = True
                self._ready.set()

    def __call__(self, frame: FrameT) -> bool:
        with self._lock:
            self._waiting = frame
            self._ready.set()
            since = self._writing_since
            stalled = since is not None and self._monotonic() - since >= self._stall_after
            return not (self._failing or stalled)

    def _run(self) -> None:
        # `_ready` is set only alongside a waiting frame or a heartbeat asked for, all under the lock, or by `stop`.
        while True:
            self._ready.wait()
            if self._stopped.is_set():
                return
            with self._lock:
                frame, self._waiting = self._waiting, None
                beat, self._beat = self._beat, False
                self._ready.clear()
                self._writing_since = self._monotonic()
            if frame is None:
                assert beat and self._heartbeat is not None  # nothing else wakes the thread
                self._attempt(self._heartbeat)
                with self._lock:
                    self._writing_since = None
                continue
            delivered = self._attempt(lambda: self._write(frame))
            with self._lock:
                self._writing_since, self._failing = None, not delivered
            if delivered:
                continue
            with self._lock:
                if self._waiting is None:
                    self._waiting = frame  # try it again, unless a newer one came meanwhile
                    self._ready.set()
            self._stopped.wait(self._retry_every)

    def _attempt(self, write: Callable[[], bool]) -> bool:
        try:
            delivered = write()
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

    `write` is the writer for a `BackgroundFrameWriter`, and `heartbeat` its heartbeat. It starts a display process
    when none is running, and a new one `respawn_after` one dropped or failed to start. A frame that doesn't go out
    (no process yet, a failed start, a broken pipe) is reported as not arrived, never raised. Each process it starts
    is a new generation in its `DisplayLink`, and gets frames with ids once it says it's ready, in the link's current
    session once it says it reads sessions. Only a receiver that reads sessions gets heartbeats: an older one would
    take one for a frame it can't read.
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
        protocol, session = self._link.protocol(child.generation)
        if protocol is LinkProtocol.SESSIONS:
            sent = encode_session_frame(session, frame.id, frame.width, frame.height, frame.pixels)
        else:
            sent = encode_frame(frame) if protocol is LinkProtocol.ACKNOWLEDGING else encode_legacy_frame(frame)
        if not self._send(child, sent):
            return False
        self._link.written(child.generation, frame.id)
        return True

    def heartbeat(self) -> bool:
        """Tell the receiver the app is running, if it reads sessions; True unless that write failed."""
        child = self._child
        if child is None or child.process.poll() is not None:
            return True  # nothing to tell: the next frame starts a display process
        protocol, session = self._link.protocol(child.generation)
        if protocol is not LinkProtocol.SESSIONS:
            return True
        return self._send(child, encode_heartbeat(session))

    def _send(self, child: _Child, data: bytes) -> bool:
        try:
            stdin = child.process.stdin
            if stdin is None:
                raise BrokenPipeError("the display process has no stdin")
            stdin.write(data)
            stdin.flush()
        except OSError as exc:
            self._drop(child, f"lost the display process ({exc})")
            return False
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
                heard=lambda reply: self._heard(generation, reply),
            )
        except (OSError, ValueError):  # its output closed under the reader
            pass
        finally:
            self._link.ended(generation)

    def _heard(self, generation: int, reply: Reply) -> None:
        """What a receiver that reads sessions said."""
        if reply.kind is ReplyKind.READY:
            assert reply.boot is not None  # a ready line names the receiver's run
            self._link.announced(generation, reply.boot)
            return
        assert reply.session is not None  # every other reply names its session
        if reply.kind is ReplyKind.SHOWN:
            assert reply.frame is not None
            self._link.shown(generation, reply.frame, session=reply.session)
        elif reply.kind is ReplyKind.ALIVE:
            self._link.alive(generation, reply.session)
        else:
            self._link.blanked(generation, reply.session)

    def _drop(self, child: _Child, why: str) -> None:
        self._log(f"display: {why}; restarting shortly")
        self._link.ended(child.generation)
        self._child = None
        self._retry_at = self._monotonic() + self._respawn_after


@dataclass(frozen=True, slots=True)
class StreamDisplay:
    """A board's receiver, driven: its process, the writer that feeds it off the frame loop, and the sink the loop
    draws to. `start` before the first frame; `stop` at the end, which closes the receiver's input."""

    process: SinkProcess
    writer: BackgroundFrameWriter[StreamFrame]
    sink: StreamDisplaySink

    def start(self) -> None:
        self.writer.start()

    def stop(self, timeout: float = 5.0) -> None:
        self.writer.stop(timeout)
        self.process.close()


def stream_display(
    command: Sequence[str],
    profile: PanelProfile,
    *,
    log: Callable[[str], None],
    spawn: Callable[[list[str]], ChildProcess] = _spawn,
    monotonic: Callable[[], float] = time.monotonic,
) -> StreamDisplay:
    """The receiver ``command`` starts, wired as a board drives it: frames written off the frame loop, heartbeats
    asked for by it, and the receiver's word gathered for the sink's receipts. ``log`` hears about the process."""
    link = DisplayLink(monotonic=monotonic)
    process = SinkProcess(command, link, spawn=spawn, monotonic=monotonic, log=log)
    writer = BackgroundFrameWriter(
        process.write,
        on_error=lambda exc: log(f"display: a frame write failed: {type(exc).__name__}: {exc}"),
        heartbeat=process.heartbeat,
        monotonic=monotonic,
    )
    return StreamDisplay(
        process, writer, StreamDisplaySink(profile, writer, link, beat=writer.beat, monotonic=monotonic)
    )
