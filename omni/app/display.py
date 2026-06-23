"""DisplaySink: commit a rendered frame to a display.

The seam between the orchestration loop and the physical panel / emulator. The loop
asks the sink for a fresh `Canvas` of the sink's profile, draws a card into it via
the `RendererRegistry`, then commits it — a card is only "shown" once commit
succeeds. A real sink wraps the LED matrix (draw, then SwapOnVSync); the recording
double here captures committed frames for tests.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omni.core.enum import PanelProfile
from omni.panels.geometry import geometry_for
from omni.renderers.canvas import Canvas, RecordingCanvas

__all__ = ["DisplaySink", "RecordingDisplaySink"]


@runtime_checkable
class DisplaySink(Protocol):
    """A display the loop draws one frame at a time onto."""

    @property
    def profile(self) -> PanelProfile: ...

    def new_frame(self) -> Canvas:
        """A fresh canvas of this sink's profile geometry to draw the next frame into."""
        ...

    def commit(self, frame: Canvas) -> None:
        """Push a drawn frame to the display."""
        ...


class RecordingDisplaySink:
    """A `DisplaySink` that keeps committed frames instead of driving hardware."""

    def __init__(self, profile: PanelProfile) -> None:
        self._profile = profile
        self.frames: list[Canvas] = []

    @property
    def profile(self) -> PanelProfile:
        return self._profile

    def new_frame(self) -> Canvas:
        geometry = geometry_for(self._profile)
        return RecordingCanvas(geometry.width, geometry.height)

    def commit(self, frame: Canvas) -> None:
        self.frames.append(frame)

    @property
    def committed(self) -> int:
        return len(self.frames)
