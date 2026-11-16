"""`BackgroundFrameWriter`: frames go out on their own thread, newest first, retried until they land."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta

from omni.app.frame_stream import BackgroundFrameWriter

_SOON = 5.0  # seconds a test waits for the writer before calling it stuck


def _until(condition: Callable[[], bool], *, within: float = _SOON) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.001)
    return False


def test_handing_over_a_frame_never_waits_and_the_newest_one_goes_out() -> None:
    written: list[bytes] = []
    writing, release = threading.Event(), threading.Event()

    def write(frame: bytes) -> bool:
        writing.set()
        assert release.wait(_SOON)
        written.append(frame)
        return True

    writer = BackgroundFrameWriter(write)
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

    writer = BackgroundFrameWriter(write, retry_every=timedelta(milliseconds=1))
    writer.start()
    writer(b"A")
    assert _until(lambda: attempts == [b"A", b"A", b"A"])
    writer.stop(_SOON)
    assert attempts == [b"A", b"A", b"A"]  # delivered on the third try, and not again


def test_a_failed_frame_gives_way_to_a_newer_one() -> None:
    attempts: list[bytes] = []
    writer: BackgroundFrameWriter

    def write(frame: bytes) -> bool:
        attempts.append(frame)
        if frame == b"A":
            writer(b"B")  # a newer frame lands while A fails
            return False
        return True

    writer = BackgroundFrameWriter(write, retry_every=timedelta(milliseconds=1))
    writer.start()
    writer(b"A")
    assert _until(lambda: attempts == [b"A", b"B"])
    writer.stop(_SOON)
    assert attempts == [b"A", b"B"]  # A wasn't tried again over B


def test_stopping_a_writer_that_never_started_is_harmless() -> None:
    writer = BackgroundFrameWriter(lambda frame: True)
    writer.stop()
