#!/usr/bin/env python3
"""Show frames read from stdin on the LED matrix: the receiver at the display end of `omni.app.frame_stream`.

Runs on the board, as root, under the system Python that has the rgbmatrix bindings, so it stays compatible with
Python 3.9. The conversation (the wire, the replies, the watchdog) is `omni/app/receiver.py`, which this loads by
its path: from ``omni/app/receiver.py`` beside this file (the board's ``~/omni`` holds a copy of the repo's
``omni/``), else from the repo this file is in. It says it's ready, shows each frame and acknowledges it once it's
on the panel, keeps at most one frame waiting (a newer one replaces it), ignores frames from a session older than
the newest it has heard, and blanks the panel, saying so, once an app that sends heartbeats has sent neither a
frame nor a heartbeat for 8 seconds, even while the frame is unchanged. An older app (``OMN2`` or ``OMNF`` frames,
no heartbeats) is served as before, without the watchdog. The panel blanks when the stream ends.

    python -m omni.app --profile quad_128x64 --sink-command "ssh omni-board sudo python3 omni/board_frame_sink.py"

The matrix defaults match the reference 2x2 board (four 64x32 panels in a U, on an Adafruit HAT).
"""

import argparse
import importlib.util
import os
import secrets
import sys
import threading
import time

from PIL import Image
from rgbmatrix import RGBMatrix, RGBMatrixOptions


def load_protocol():
    """`omni/app/receiver.py`, loaded on its own: the ``omni`` package needs a newer Python than the board's."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, "omni", "app", "receiver.py"),
        os.path.join(here, "..", "omni", "app", "receiver.py"),
    ):
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("omni_receiver", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["omni_receiver"] = module  # its dataclasses look their module up
            spec.loader.exec_module(module)
            return module
    raise SystemExit("board_frame_sink: omni/app/receiver.py isn't beside it; copy it from the repo")


def say(stream, replies):
    """Tell the app something. An app that isn't listening (an older one) doesn't stop the panel."""
    try:
        for reply in replies:
            stream.write(reply.line())
        stream.flush()
    except OSError:
        pass


def matrix_options(args):
    options = RGBMatrixOptions()
    options.hardware_mapping = args.gpio_mapping
    options.rows = args.rows
    options.cols = args.cols
    options.chain_length = args.chain
    options.parallel = 1
    options.pixel_mapper_config = args.pixel_mapper
    options.gpio_slowdown = args.slowdown
    options.pwm_bits = args.pwm_bits
    options.brightness = args.brightness
    if hasattr(options, "limit_refresh_rate_hz"):
        options.limit_refresh_rate_hz = args.limit_refresh
    return options


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brightness", type=int, default=100)
    parser.add_argument("--gpio-mapping", default="adafruit-hat-pwm")
    parser.add_argument("--rows", type=int, default=32)
    parser.add_argument("--cols", type=int, default=64)
    parser.add_argument("--chain", type=int, default=4)
    parser.add_argument("--pixel-mapper", default="U-mapper")
    parser.add_argument("--slowdown", type=int, default=3)
    parser.add_argument("--pwm-bits", type=int, default=6)
    parser.add_argument("--limit-refresh", type=int, default=128)
    args = parser.parse_args()

    protocol = load_protocol()
    matrix = RGBMatrix(options=matrix_options(args))
    canvas = matrix.CreateFrameCanvas()
    receiver = protocol.Receiver(secrets.token_hex(8))
    replies = sys.stdout.buffer
    changed = threading.Condition()
    ended = []

    def read():
        """Read the app's messages on a thread of its own, so a frame waiting never holds up the panel."""
        try:
            while True:
                message = protocol.read_message(sys.stdin.buffer)
                if message is None:
                    break
                with changed:
                    said = receiver.heard(message, time.monotonic())
                    changed.notify()
                say(replies, said)
        except protocol.OutOfSync:
            sys.stderr.write("board_frame_sink: lost frame sync, stopping\n")
        finally:
            with changed:
                ended.append(True)
                changed.notify()

    say(replies, receiver.hello())
    threading.Thread(target=read, name="frames", daemon=True).start()
    try:
        while True:
            with changed:
                deadline = receiver.deadline()
                timeout = max(deadline - time.monotonic(), 0.0) if deadline is not None else None
                changed.wait_for(lambda: receiver.waiting or ended, timeout)
                frame = receiver.take()
                blank = receiver.expired(time.monotonic())
            if blank:
                matrix.Clear()
                say(replies, blank)
            if frame is not None:
                canvas.SetImage(Image.frombytes("RGB", (frame.width, frame.height), frame.pixels))
                canvas = matrix.SwapOnVSync(canvas)
                with changed:
                    said = receiver.swapped(frame)
                say(replies, said)
            elif ended:
                break
    except KeyboardInterrupt:
        pass
    finally:
        matrix.Clear()


if __name__ == "__main__":
    main()
