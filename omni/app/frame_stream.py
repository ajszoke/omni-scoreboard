"""Frames as raw RGB bytes down a pipe, to a display process on the other end.

The LED matrix library needs root and the board's system Python, while the app needs neither. So the app
can draw into memory and hand each finished frame to a small display process (`tools/board_frame_sink.py`)
that owns the panel. The two can share a board, or the display process can sit across an SSH session.

Each frame is the 4-byte magic ``OMNF``, the width and height as little-endian 16-bit integers, then the
pixels row by row, three bytes each. A frame identical to the last one sent isn't sent again, unless the
display process on the other end is a new one (a new ``generation``), which starts with a blank panel.

Writing to a pipe can block: across SSH, a stalled link stops the other end reading. A
`BackgroundFrameWriter` takes the writing off the frame loop, so drawing goes on while a frame waits to
go out.

A frame counts as shown only while the display is taking frames: `StreamDisplaySink.commit` raises
`DisplayUnavailable` when the writer says a frame didn't go out, so a board spends no airtime on frames
nobody saw. (That a frame reached the LEDs themselves is the display process's to prove.)
"""

from __future__ import annotations

import struct
import threading
import time
from collections.abc import Callable
from datetime import timedelta

from omni.core.enum import PanelProfile
from omni.panels.geometry import geometry_for
from omni.renderers.canvas import Canvas
from omni.renderers.pillow_canvas import PillowCanvas

__all__ = ["FRAME_MAGIC", "BackgroundFrameWriter", "DisplayUnavailable", "StreamDisplaySink", "encode_frame"]

FRAME_MAGIC = b"OMNF"

# Sends one encoded frame on; False means it didn't arrive and the next frame should be sent regardless.
FrameWriter = Callable[[bytes], bool]
# Which display process is on the other end: a change means a fresh one that has drawn nothing yet.
SinkGeneration = Callable[[], int]


class DisplayUnavailable(OSError):
    """The display isn't taking frames: the frame just committed won't be seen."""


def encode_frame(width: int, height: int, pixels: bytes) -> bytes:
    """One frame on the wire."""
    if len(pixels) != width * height * 3:
        raise ValueError("a frame is three bytes per pixel")
    return FRAME_MAGIC + struct.pack("<HH", width, height) + pixels


class StreamDisplaySink:
    """A `DisplaySink` that draws in memory and writes each changed frame to a `FrameWriter`."""

    def __init__(self, profile: PanelProfile, write: FrameWriter, generation: SinkGeneration | None = None) -> None:
        self._profile = profile
        self._write = write
        self._generation = generation
        self._sent: bytes | None = None
        self._sent_to: int | None = None  # the generation that got ``_sent``

    @property
    def profile(self) -> PanelProfile:
        return self._profile

    def new_frame(self) -> Canvas:
        geometry = geometry_for(self._profile)
        return PillowCanvas(geometry.width, geometry.height)

    def commit(self, frame: Canvas) -> None:
        if not isinstance(frame, PillowCanvas):
            raise TypeError("StreamDisplaySink.commit expects a PillowCanvas from new_frame()")
        pixels = frame.image().tobytes()
        generation = self._generation() if self._generation is not None else None
        if pixels == self._sent and generation == self._sent_to:
            return
        delivered = self._write(encode_frame(frame.width, frame.height, pixels))
        self._sent = pixels if delivered else None
        self._sent_to = generation
        if not delivered:
            raise DisplayUnavailable("the display isn't taking frames")


class BackgroundFrameWriter:
    """A `FrameWriter` that writes on its own thread, newest frame first.

    Only the newest frame waits: one handed over while another is going out replaces any still waiting.
    A frame that doesn't arrive is tried again until it does, or until a newer one replaces it, so the
    display ends up on the newest frame either way. Handing a frame over never blocks. It reports the
    frame as going out unless the writer is failing (its last write didn't arrive) or stalled (a write
    has been going out for `stall_after` or longer), so the caller knows when the display isn't taking
    frames.
    """

    def __init__(
        self,
        write: FrameWriter,
        *,
        retry_every: timedelta = timedelta(milliseconds=500),
        stall_after: timedelta = timedelta(seconds=2),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._write = write
        self._retry_every = retry_every.total_seconds()
        self._stall_after = stall_after.total_seconds()
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._failing = False  # the last write didn't arrive, and none has since
        self._writing_since: float | None = None  # when the write going out now began
        self._waiting: bytes | None = None
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

    def __call__(self, frame: bytes) -> bool:
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
            delivered = self._write(frame)
            with self._lock:
                self._writing_since, self._failing = None, not delivered
            if delivered:
                continue
            with self._lock:
                if self._waiting is None:
                    self._waiting = frame  # try it again, unless a newer one came meanwhile
                    self._ready.set()
            self._stopped.wait(self._retry_every)
