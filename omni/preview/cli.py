"""`python -m omni.preview` — draw a fixture card on the RGBMatrixEmulator.

    python -m omni.preview --profile quad_128x64 --fixture fixtures/mlb/live-close-game.json

The emulator serves a browser at http://localhost:8888/. By default the preview
holds until interrupted; pass `--duration N` to exit after N seconds.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any, Sequence

from omni.core.enum import PanelProfile
from omni.panels.geometry import geometry_for
from omni.preview.scenario import build_card_from_scenario
from omni.renderers.context import RenderContext
from omni.renderers.matrix_canvas import MatrixCanvas
from omni.renderers.registry import default_registry

__all__ = ["main"]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="omni.preview", description="Preview a card on the LED matrix emulator.")
    parser.add_argument("--profile", required=True, choices=[profile.value for profile in PanelProfile])
    parser.add_argument("--fixture", required=True, help="path to a preview scenario JSON (schedule row + game feed)")
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="seconds to hold the frame before exiting; default holds until interrupted",
    )
    return parser.parse_args(argv)


def _configure_options(options: Any, profile: PanelProfile) -> Any:
    """Set panel geometry on an RGBMatrixOptions for the profile's logical size.

    Physical multi-panel mapping (e.g. 2x2 64x32 -> 128x64) is real-hardware
    config; the emulator just needs the logical width/height.
    """
    width, height = geometry_for(profile).size
    options.cols = width
    options.rows = height
    options.chain_length = 1
    options.parallel = 1
    return options


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - drives the live emulator
    args = _parse_args(argv)
    profile = PanelProfile(args.profile)
    card = build_card_from_scenario(args.fixture, now=datetime.now(timezone.utc))

    from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions

    width, height = geometry_for(profile).size
    matrix = RGBMatrix(options=_configure_options(RGBMatrixOptions(), profile))
    frame = matrix.CreateFrameCanvas()
    default_registry().render(card, RenderContext(profile=profile), MatrixCanvas(frame, width, height))
    matrix.SwapOnVSync(frame)

    import time

    if args.duration is None:
        while True:
            time.sleep(1)
    time.sleep(args.duration)
    return 0
