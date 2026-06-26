"""Overflow-safe text lanes: clock-driven marquee math, glyph clipping, and anchoring.

The point of the primitive is that the scroll position is a pure function of the render clock,
so a frame never stutters or resets just because the render loop fired more or fewer times —
the determinism tests below pin that down.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.core.colors import RGBColor
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.text import text_width
from omni.renderers.text_lane import (
    DEFAULT_MARQUEE,
    LaneAnchor,
    MarqueePlan,
    MarqueeStyle,
    TextLane,
    _clip_to_lane,
    draw_text_lane,
)

NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
_WHITE = RGBColor(255, 255, 255)

# A style with exact arithmetic so the offset math can be asserted to the pixel.
_EXACT = MarqueeStyle(speed_px_per_s=10.0, start_dwell_s=2.0, end_dwell_s=3.0)
_PLAN = MarqueePlan(distance=50, style=_EXACT)  # scroll 5.0s, period 10.0s


# --- timing knobs validate their inputs -------------------------------------------------------


def test_default_marquee_is_the_zero_arg_style() -> None:
    assert DEFAULT_MARQUEE == MarqueeStyle()


@pytest.mark.parametrize("speed", [0.0, -1.0])
def test_a_non_positive_speed_is_rejected(speed: float) -> None:
    with pytest.raises(ValueError, match="speed"):
        MarqueeStyle(speed_px_per_s=speed)


@pytest.mark.parametrize("start, end", [(-1.0, 0.0), (0.0, -1.0)])
def test_a_negative_dwell_is_rejected(start: float, end: float) -> None:
    with pytest.raises(ValueError, match="dwell"):
        MarqueeStyle(start_dwell_s=start, end_dwell_s=end)


def test_a_plan_needs_a_positive_distance() -> None:
    with pytest.raises(ValueError, match="distance"):
        MarqueePlan(distance=0)


def test_a_lane_needs_a_positive_width() -> None:
    with pytest.raises(ValueError, match="width"):
        TextLane(x=0, y=0, width=0, font="4x6")


# --- the offset is a deterministic, cyclic function of the clock -------------------------------


def test_period_and_scroll_durations() -> None:
    assert (_PLAN.scroll_s, _PLAN.period_s) == (5.0, 10.0)


@pytest.mark.parametrize(
    "clock_s, offset",
    [
        (0.0, 0),  # head dwell
        (1.9, 0),  # still the head dwell
        (2.0, 0),  # slide begins at the head
        (3.0, 10),  # 1.0s into the slide at 10px/s
        (4.0, 20),  # 2.0s in
        (6.0, 40),  # 4.0s in
        (7.0, 50),  # slide done -> tail dwell pins to distance
        (9.9, 50),  # still the tail dwell
    ],
)
def test_offset_walks_dwell_slide_dwell(clock_s: float, offset: int) -> None:
    assert _PLAN.offset_at(clock_s) == offset


def test_offset_is_cyclic_with_the_period() -> None:
    # Same phase one and two periods later -> identical offset (no drift, no accumulation).
    assert _PLAN.offset_at(4.0) == _PLAN.offset_at(14.0) == _PLAN.offset_at(24.0) == 20


def test_offset_is_monotonic_through_the_slide() -> None:
    offsets = [_PLAN.offset_at(t) for t in (2.0, 3.0, 4.0, 6.0, 7.0)]
    assert offsets == sorted(offsets)


def test_offset_is_pure_in_its_argument() -> None:
    # The whole reason for a clock-driven scroll: the same instant always yields the same frame.
    assert _PLAN.offset_at(3.3) == _PLAN.offset_at(3.3)


# --- glyph clipping keeps the run wholly inside the lane ---------------------------------------


def test_at_the_head_the_leading_run_is_shown_from_the_lane_left() -> None:
    lane = TextLane(x=10, y=0, width=20, font="4x6")  # 20px lane fits 5 of the 4px cells
    visible, vx = _clip_to_lane("ABCDEFGH", lane, offset=0)
    assert (visible, vx) == ("ABCDE", 10)
    assert vx >= lane.x and vx + text_width(visible, "4x6") <= lane.right


def test_at_the_tail_the_trailing_run_is_flush_to_the_lane_right() -> None:
    lane = TextLane(x=10, y=0, width=20, font="4x6")
    distance = text_width("ABCDEFGH", "4x6") - lane.width  # 32 - 20 = 12
    visible, vx = _clip_to_lane("ABCDEFGH", lane, offset=distance)
    assert visible == "DEFGH"  # leading glyphs scrolled off the left; loop runs to the end
    assert vx + text_width(visible, "4x6") == lane.right  # the tail sits flush on the right edge


def test_a_lane_narrower_than_a_glyph_keeps_nothing() -> None:
    lane = TextLane(x=0, y=0, width=3, font="4x6")  # every 4px cell overflows a 3px lane
    assert _clip_to_lane("AB", lane, offset=0) == ("", 0)


# --- draw routing: fit -> anchor, overflow -> clipped marquee ----------------------------------


@pytest.mark.parametrize(
    "anchor, expected_x",
    [
        (LaneAnchor.LEFT, 10),  # at the lane's left
        (LaneAnchor.RIGHT, 30 - 8),  # right edge (x+width=30) minus the 8px string
        (LaneAnchor.CENTER, 10 + (20 - 8) // 2),  # centered in the 20px lane
    ],
)
def test_text_that_fits_is_anchored_whole(anchor: LaneAnchor, expected_x: int) -> None:
    lane = TextLane(x=10, y=4, width=20, font="4x6", anchor=anchor)
    canvas = RecordingCanvas(64, 16)
    draw_text_lane(canvas, lane, "AB", _WHITE, now=NOW)  # "AB" is 8px, fits the 20px lane
    ops = canvas.texts()
    assert len(ops) == 1
    assert (ops[0].text, ops[0].x, ops[0].y) == ("AB", expected_x, 4)


def test_overflow_draws_the_clock_clipped_substring_without_spilling() -> None:
    lane = TextLane(x=10, y=2, width=20, font="4x6")
    text = "PITCHER NAME LONG"
    canvas = RecordingCanvas(64, 16)
    draw_text_lane(canvas, lane, text, _WHITE, now=NOW)

    # It must draw exactly what the clock-derived offset clips to — no more, no less.
    plan = MarqueePlan(distance=text_width(text, "4x6") - lane.width)
    visible, vx = _clip_to_lane(text, lane, plan.offset_at(NOW.timestamp()))
    ops = canvas.texts()
    assert len(ops) == 1
    assert (ops[0].text, ops[0].x, ops[0].y) == (visible, vx, 2)
    # ...and the drawn run never reaches past either edge of the lane.
    assert ops[0].x >= lane.x
    assert ops[0].x + text_width(ops[0].text, lane.font) <= lane.right


def test_the_same_clock_redraws_an_identical_frame() -> None:
    # No hidden per-frame state: rendering twice at one instant cannot drift or reset the scroll.
    lane = TextLane(x=0, y=0, width=18, font="4x6")
    text = "A VERY LONG ROSTER LINE"
    first, second = RecordingCanvas(64, 16), RecordingCanvas(64, 16)
    draw_text_lane(first, lane, text, _WHITE, now=NOW)
    draw_text_lane(second, lane, text, _WHITE, now=NOW)
    assert first.ops == second.ops


def test_a_lane_narrower_than_its_glyphs_draws_blank_rather_than_spilling() -> None:
    lane = TextLane(x=0, y=0, width=3, font="4x6")  # forces overflow, but nothing fits
    canvas = RecordingCanvas(64, 16)
    draw_text_lane(canvas, lane, "AB", _WHITE, now=NOW)
    assert canvas.texts() == []
