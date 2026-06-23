"""Tests for MatrixDisplaySink (the rgbmatrix/emulator-backed DisplaySink)."""

from __future__ import annotations

import pytest

from omni.app.display import MatrixDisplaySink
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.matrix_canvas import MatrixCanvas


class _FakeFrame:
    """A MatrixSurface that records the pixels set on it."""

    def __init__(self) -> None:
        self.pixels: dict[tuple[int, int], tuple[int, int, int]] = {}

    def SetPixel(self, x: int, y: int, r: int, g: int, b: int) -> None:
        self.pixels[(x, y)] = (r, g, b)


class _FakeMatrix:
    """A MatrixDevice double: hands out fresh frames and records swaps."""

    def __init__(self) -> None:
        self.swapped: list[object] = []

    def CreateFrameCanvas(self) -> _FakeFrame:
        return _FakeFrame()

    def SwapOnVSync(self, frame: object) -> None:
        self.swapped.append(frame)


def test_new_frame_matches_profile_geometry() -> None:
    sink = MatrixDisplaySink(_FakeMatrix(), PanelProfile.QUAD_128X64)
    assert sink.profile is PanelProfile.QUAD_128X64
    frame = sink.new_frame()
    assert isinstance(frame, MatrixCanvas)
    assert (frame.width, frame.height) == (128, 64)


def test_commit_swaps_the_drawn_frame() -> None:
    matrix = _FakeMatrix()
    sink = MatrixDisplaySink(matrix, PanelProfile.STACK_64X64)
    frame = sink.new_frame()
    assert isinstance(frame, MatrixCanvas)
    frame.set_pixel(1, 2, RGBColor(255, 0, 0))
    sink.commit(frame)
    assert matrix.swapped == [frame.surface]  # the underlying frame canvas was presented
    surface = frame.surface
    assert isinstance(surface, _FakeFrame)
    assert surface.pixels[(1, 2)] == (255, 0, 0)  # what we drew reached the device


def test_commit_rejects_a_foreign_canvas() -> None:
    sink = MatrixDisplaySink(_FakeMatrix(), PanelProfile.SINGLE_64X32)
    with pytest.raises(TypeError, match="expects a MatrixCanvas"):
        sink.commit(RecordingCanvas(64, 32))
