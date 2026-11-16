"""The board's explicit frames (idle, held, offline) and a stale card, drawn on every profile.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k "board_frame"
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from omni.core.enum import PanelProfile
from omni.panels.geometry import geometry_for
from omni.renderers.board_frame import BoardFrameRenderer, clock_label
from omni.renderers.canvas import DimmedCanvas, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.live_baseball import LiveBaseballRenderer
from omni.renderers.pillow_canvas import PillowCanvas
from omni.tower.board import BoardFrame, FrameKind
from tests.test_renderer_live_baseball import LOGOS, make_card

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
ALL_PROFILES = (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32)
NOW = datetime(2026, 6, 18, 1, 42, tzinfo=timezone.utc)  # 7:42 PM in Denver
DENVER = ZoneInfo("America/Denver")
_FRAMES = {
    FrameKind.IDLE: BoardFrame(FrameKind.IDLE, "nothing eligible"),
    FrameKind.HELD: BoardFrame(FrameKind.HELD, "2 games in the TV delay"),
    FrameKind.OFFLINE: BoardFrame(FrameKind.OFFLINE, "no schedule for over 10m: down"),
}


def _assert_matches_golden(image: Image.Image, name: str) -> None:
    path = GOLDEN_DIR / name
    if os.environ.get("OMNI_REGEN_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return
    assert path.exists(), f"missing golden {name}; regenerate with OMNI_REGEN_GOLDEN=1"
    with Image.open(path) as expected:
        assert image.convert("RGB").tobytes() == expected.convert("RGB").tobytes(), f"render differs from golden {name}"


def test_the_clock_reads_like_a_board_clock_in_its_zone() -> None:
    assert clock_label(NOW, DENVER) == "7:42 PM"
    assert clock_label(datetime(2026, 6, 18, 6, 5, tzinfo=timezone.utc), DENVER) == "12:05 AM"
    assert clock_label(datetime(2026, 6, 18, 18, 0, tzinfo=timezone.utc), DENVER) == "12:00 PM"
    assert clock_label(NOW, None) == "1:42 AM"  # no zone: the render clock's own (UTC here)


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_every_frame_is_the_clock_and_all_but_idle_say_why(profile: PanelProfile) -> None:
    width, height = geometry_for(profile).size
    for kind, frame in _FRAMES.items():
        canvas = RecordingCanvas(width, height)
        BoardFrameRenderer().render(frame, RenderContext(profile=profile, now=NOW, timezone=DENVER), canvas)
        texts = [op.text for op in canvas.texts()]
        labels = {FrameKind.IDLE: [], FrameKind.HELD: ["TV DELAY"], FrameKind.OFFLINE: ["OFFLINE"]}[kind]
        assert texts == ["7:42 PM", *labels]
        assert all(0 <= op.x and op.y + 6 <= height for op in canvas.texts())  # on the panel


@pytest.mark.parametrize("profile", ALL_PROFILES)
@pytest.mark.parametrize("kind", list(FrameKind))
def test_golden_board_frame(profile: PanelProfile, kind: FrameKind) -> None:
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    BoardFrameRenderer().render(_FRAMES[kind], RenderContext(profile=profile, now=NOW, timezone=DENVER), canvas)
    _assert_matches_golden(canvas.image(), f"board_frame_{kind.value}_{profile.to_json_value()}.png")


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_golden_stale_live_card(profile: PanelProfile) -> None:
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    context = RenderContext(profile=profile, now=NOW, logos=LOGOS)
    LiveBaseballRenderer().render(make_card(), context, DimmedCanvas(canvas))
    _assert_matches_golden(canvas.image(), f"board_stale_live_baseball_{profile.to_json_value()}.png")
