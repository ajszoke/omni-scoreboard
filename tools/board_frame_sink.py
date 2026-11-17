#!/usr/bin/env python3
"""Show frames read from stdin on the LED matrix: the display end of `omni.app.frame_stream`.

Runs on the board, as root, under the system Python that has the rgbmatrix bindings, so it stays
compatible with Python 3.9. Each frame is ``OMN2``, the frame's id as a little-endian 32-bit integer, the
width and height as little-endian 16-bit integers, then width x height RGB pixels. Once the matrix is up
it says ``OMN2 READY`` on stdout, and once each frame is on the panel, ``ACK <id>``. An older app sends
``OMNF`` frames (no id), which are shown and not acknowledged. The panel blanks when the stream ends.

    python -m omni.app.football ... --sink-command "ssh omni-board sudo python3 omni/board_frame_sink.py"

The matrix defaults match the reference 2x2 board (four 64x32 panels in a U, on an Adafruit HAT).
"""

import argparse
import struct
import sys

from PIL import Image
from rgbmatrix import RGBMatrix, RGBMatrixOptions

MAGIC = b"OMN2"  # then the id, width and height: "<IHH"
LEGACY_MAGIC = b"OMNF"  # then the width and height: "<HH"
READY = b"OMN2 READY\n"


def read_exact(stream, size):
    """Exactly ``size`` bytes, or None if the stream ends first."""
    chunks = []
    left = size
    while left:
        chunk = stream.read(left)
        if not chunk:
            return None
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def read_header(stream):
    """The next frame's (id, width, height), id None for an ``OMNF`` frame; None at the end or out of sync."""
    magic = read_exact(stream, 4)
    if magic == MAGIC:
        rest = read_exact(stream, 8)
        return struct.unpack("<IHH", rest) if rest is not None else None
    if magic == LEGACY_MAGIC:
        rest = read_exact(stream, 4)
        return (None,) + struct.unpack("<HH", rest) if rest is not None else None
    if magic is not None:
        sys.stderr.write("board_frame_sink: lost frame sync, stopping\n")
    return None


def reply(stream, line):
    """Tell the app something. An app that isn't listening (an older one) doesn't stop the panel."""
    try:
        stream.write(line)
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

    matrix = RGBMatrix(options=matrix_options(args))
    canvas = matrix.CreateFrameCanvas()
    stream = sys.stdin.buffer
    replies = sys.stdout.buffer
    reply(replies, READY)
    try:
        while True:
            header = read_header(stream)
            if header is None:
                break
            frame_id, width, height = header
            pixels = read_exact(stream, width * height * 3)
            if pixels is None:
                break
            canvas.SetImage(Image.frombytes("RGB", (width, height), pixels))
            canvas = matrix.SwapOnVSync(canvas)
            if frame_id is not None:
                reply(replies, b"ACK %d\n" % frame_id)
    except KeyboardInterrupt:
        pass
    finally:
        matrix.Clear()


if __name__ == "__main__":
    main()
