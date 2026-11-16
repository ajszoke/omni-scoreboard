#!/usr/bin/env python3
"""Show frames read from stdin on the LED matrix: the display end of `omni.app.frame_stream`.

Runs on the board, as root, under the system Python that has the rgbmatrix bindings, so it stays
compatible with Python 3.9. Each frame is ``OMNF``, the width and height as little-endian 16-bit integers,
then width x height RGB pixels. The panel blanks when the stream ends.

    python -m omni.app.football ... --sink-command "ssh omni-board sudo python3 omni/board_frame_sink.py"

The matrix defaults match the reference 2x2 board (four 64x32 panels in a U, on an Adafruit HAT).
"""

import argparse
import struct
import sys

from PIL import Image
from rgbmatrix import RGBMatrix, RGBMatrixOptions

MAGIC = b"OMNF"


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
    try:
        while True:
            header = read_exact(stream, 8)
            if header is None:
                break
            if header[:4] != MAGIC:
                sys.stderr.write("board_frame_sink: lost frame sync, stopping\n")
                break
            width, height = struct.unpack("<HH", header[4:])
            pixels = read_exact(stream, width * height * 3)
            if pixels is None:
                break
            canvas.SetImage(Image.frombytes("RGB", (width, height), pixels))
            canvas = matrix.SwapOnVSync(canvas)
    except KeyboardInterrupt:
        pass
    finally:
        matrix.Clear()


if __name__ == "__main__":
    main()
