"""The board's explicit frames (idle, held, offline) and a stale card, drawn on every profile.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k "board_frame"
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
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
    # 20 s into a 45 s wait: the bar is 4/9 full.
    FrameKind.HELD: BoardFrame(
        FrameKind.HELD, "2 games in the TV delay", NOW - timedelta(seconds=20), NOW + timedelta(seconds=25)
    ),
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
        labels = {FrameKind.IDLE: [], FrameKind.HELD: ["Syncing..."], FrameKind.OFFLINE: ["OFFLINE"]}[kind]
        assert texts == ["7:42 PM", *labels]
        assert all(0 <= op.x and op.y + 6 <= height for op in canvas.texts())  # on the panel


@pytest.mark.parametrize(
    ("profile", "bar"),
    [
        (PanelProfile.QUAD_128X64, (28, 47, 72, 4, 32)),
        (PanelProfile.STACK_64X64, (10, 42, 44, 3, 19)),
        (PanelProfile.SINGLE_64X32, (0, 30, 64, 2, 28)),  # the whole bottom edge
    ],
)
def test_a_held_frame_fills_its_bar_through_the_wait(profile: PanelProfile, bar: tuple[int, ...]) -> None:
    x, y, width, height, filled = bar
    canvas = RecordingCanvas(*geometry_for(profile).size)
    BoardFrameRenderer().render(_FRAMES[FrameKind.HELD], RenderContext(profile=profile, now=NOW), canvas)
    track, fill = canvas.rects()
    assert (track.x, track.y, track.w, track.h) == (x, y, width, height)
    assert (fill.x, fill.y, fill.w, fill.h) == (x, y, filled, height)  # 4/9 of the way


def test_a_held_frame_with_no_known_end_shows_an_empty_track() -> None:
    unknown = BoardFrame(FrameKind.HELD, "a game in the TV delay", NOW)
    canvas = RecordingCanvas(128, 64)
    BoardFrameRenderer().render(unknown, RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), canvas)
    assert [(rect.w, rect.h) for rect in canvas.rects()] == [(72, 4)]


def test_a_wait_s_progress_runs_from_zero_to_one() -> None:
    held = _FRAMES[FrameKind.HELD]
    assert held.progress(NOW - timedelta(seconds=30)) == 0.0
    assert held.progress(NOW + timedelta(seconds=60)) == 1.0
    assert BoardFrame(FrameKind.HELD, "-", NOW, NOW).progress(NOW) == 1.0  # due already
    assert BoardFrame(FrameKind.IDLE, "-").progress(NOW) is None


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
