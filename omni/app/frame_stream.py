"""Frames as raw RGB bytes down a pipe, to a display process on the other end.

The LED matrix library needs root and the board's system Python, while the app needs neither. So the app
can draw into memory and hand each finished frame to a small display process (`tools/board_frame_sink.py`)
that owns the panel. The two can share a board, or the display process can sit across an SSH session.

Each frame is the 4-byte magic ``OMNF``, the width and height as little-endian 16-bit integers, then the
pixels row by row, three bytes each. A frame identical to the last one sent isn't sent again, unless the
display process on the other end is a new one (a new ``generation``), which starts with a blank panel.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

from omni.core.enum import PanelProfile
from omni.panels.geometry import geometry_for
from omni.renderers.canvas import Canvas
from omni.renderers.pillow_canvas import PillowCanvas

__all__ = ["FRAME_MAGIC", "StreamDisplaySink", "encode_frame"]

FRAME_MAGIC = b"OMNF"

# Sends one encoded frame on; False means it didn't arrive and the next frame should be sent regardless.
FrameWriter = Callable[[bytes], bool]
# Which display process is on the other end: a change means a fresh one that has drawn nothing yet.
SinkGeneration = Callable[[], int]


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
