"""Overflow-safe text lanes: clock-driven marquee math, glyph clipping, and anchoring.

The point of the primitive is that the scroll position is a pure function of the render clock,
so a frame never stutters or resets just because the render loop fired more or fewer times —
the determinism tests below pin that down.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from omni.core.clock import MonotonicTime
from omni.core.colors import RGBColor
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.text import text_width
from omni.renderers.text_lane import (
    DEFAULT_MARQUEE,
    LaneAnchor,
    MarqueePlan,
    MarqueeStyle,
    LaneClock,
    Marquee,
    ScrollSpeed,
    TextLane,
    _clip_to_lane,
    draw_text_lane,
)

NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
_WHITE = RGBColor(255, 255, 255)

# A style with exact arithmetic so the offset math can be asserted to the pixel: it reaches 10 px/s
# over a 1 s ramp (covering 5 px), and holds 2 s at each end.
_EXACT = MarqueeStyle(cruise_px_per_s=10.0, ramp_s=1.0, hold_s=2.0)
_PLAN = MarqueePlan(distance=50, style=_EXACT)  # ramp 1 s, cruise 4 s, ramp 1 s: a 6 s trip


# --- timing knobs validate their inputs -------------------------------------------------------


def test_default_marquee_is_the_zero_arg_style() -> None:
    assert DEFAULT_MARQUEE == MarqueeStyle()


@pytest.mark.parametrize(
    ("knobs", "complaint"),
    [
        ({"cruise_px_per_s": 0.0}, "cruise"),
        ({"cruise_px_per_s": -1.0}, "cruise"),
        ({"ramp_s": 0.0}, "ramp"),
        ({"hold_s": -1.0}, "hold"),
    ],
)
def test_a_bad_knob_is_rejected(knobs: dict[str, float], complaint: str) -> None:
    with pytest.raises(ValueError, match=complaint):
        MarqueeStyle(**knobs)


def test_a_plan_needs_a_positive_distance() -> None:
    with pytest.raises(ValueError, match="distance"):
        MarqueePlan(distance=0)


def test_a_lane_needs_a_positive_width() -> None:
    with pytest.raises(ValueError, match="width"):
        TextLane(x=0, y=0, width=0, font="4x6")


# --- the offset is a deterministic, cyclic function of the clock -------------------------------


def test_period_and_travel_durations() -> None:
    assert (_EXACT.acceleration, _EXACT.ramp_px) == (10.0, 5.0)
    assert (_PLAN.ramp_time_s, _PLAN.travel_s, _PLAN.period_s) == (1.0, 6.0, 16.0)  # hold 2, out 6, hold 2, back 6


@pytest.mark.parametrize(
    "clock_s, offset",
    [
        (0.0, 0),  # holding at the head
        (1.9, 0),  # still holding
        (2.0, 0),  # the trip out begins at the head
        (2.5, 1),  # speeding up along the ramp
        (3.0, 5),  # at cruising speed, one ramp's worth along
        (5.0, 25),  # halfway in time is halfway in distance
        (7.0, 45),  # the slowing ramp begins
        (7.5, 49),
        (8.0, 50),  # arrived -> holding at the tail
        (9.9, 50),  # still holding
        (10.0, 50),  # the trip back begins at the tail
        (10.5, 49),  # a mirror of the trip out
        (13.0, 25),
        (15.5, 1),
        (16.0, 0),  # home, and the cycle repeats
    ],
)
def test_offset_holds_ramps_cruises_and_comes_back(clock_s: float, offset: int) -> None:
    assert _PLAN.offset_at(clock_s) == offset


def test_every_length_holds_and_ramps_the_same_way() -> None:
    short, long = MarqueePlan(distance=30, style=_EXACT), MarqueePlan(distance=300, style=_EXACT)
    head = [step / 10 for step in range(31)]  # the hold and the whole ramp
    assert [short.offset_at(t) for t in head] == [long.offset_at(t) for t in head]
    assert long.travel_s - short.travel_s == pytest.approx(27.0)  # only the cruise stretches


def test_a_trip_too_short_to_reach_cruising_speed_turns_around_in_the_middle() -> None:
    plan = MarqueePlan(distance=2, style=_EXACT)
    assert plan.ramp_time_s == pytest.approx(0.2**0.5)
    assert plan.travel_s == pytest.approx(2 * 0.2**0.5)
    assert plan.offset_at(_EXACT.hold_s + plan.ramp_time_s) == 1  # halfway, at the turn


def test_offset_is_cyclic_with_the_period() -> None:
    # Same phase one and two periods later -> identical offset (no drift, no accumulation).
    assert _PLAN.offset_at(5.0) == _PLAN.offset_at(21.0) == _PLAN.offset_at(37.0) == 25


def test_offset_never_jumps_on_the_way_back() -> None:
    # The return trip retraces the path, rather than the text snapping back to the head.
    samples = [_PLAN.offset_at(10.0 + i * 0.05) for i in range(121)]
    assert samples == sorted(samples, reverse=True)
    assert max(abs(a - b) for a, b in zip(samples, samples[1:])) <= 1  # 0.05s at 10 px/s is half a pixel


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


def _drawn(
    lane: TextLane, text: str, seconds: float, clock: LaneClock, *, presentation: str = "card", commit: bool = True
) -> tuple[int, str] | None:
    """Draw ``text`` in a frame of ``presentation`` at ``seconds`` on the monotonic clock; where it landed."""
    frame = clock.frame(presentation, MonotonicTime(seconds))
    canvas = RecordingCanvas(64, 8)
    draw_text_lane(canvas, lane, text, _WHITE, now=NOW, clock=frame)
    if commit:
        frame.commit()
    texts = [op for op in canvas.ops if op.text]
    return (texts[0].x, texts[0].text) if texts else None


_LONG = "A LONG LINE THAT OVERFLOWS ITS LANE"
_LANE = TextLane(x=0, y=0, width=20, font="4x6")


def test_with_a_lane_clock_a_line_starts_at_its_head_whatever_the_clock_says() -> None:
    for start in (0.0, 7123.3):
        clock = LaneClock()
        first = _drawn(_LANE, _LONG, start, clock)
        assert first is not None and first[0] == 0 and _LONG.startswith(first[1])
        moving = [_drawn(_LANE, _LONG, start + t / 10, clock) for t in range(1, 60)]
        assert moving[15] == first  # still holding at 1.6s
        assert moving[-1] != first  # moving by 6s


def test_a_changed_line_starts_over_and_a_replaced_scroll_leaves_a_beat_of_nothing() -> None:
    clock = LaneClock()
    for tenth in range(80):  # the first line scrolls a while
        _drawn(_LANE, _LONG, tenth / 10, clock)
    assert _drawn(_LANE, "AND ANOTHER LONG LINE ENTIRELY", 8.0, clock) is None  # the beat between lines
    after = _drawn(_LANE, "AND ANOTHER LONG LINE ENTIRELY", 8.3, clock)
    assert after is not None and after[0] == 0 and after[1].startswith("AND")  # at its own head
    assert _drawn(_LANE, "OK", 8.4, clock) is None  # still a beat: the line it replaced scrolled
    assert _drawn(_LANE, "OK", 8.7, clock) == (0, "OK")
    assert _drawn(_LANE, "NO", 8.8, clock) == (0, "NO")  # a short line swaps at once


def test_a_line_keeps_its_place_however_long_its_lane_goes_undrawn() -> None:
    clock = LaneClock()
    _drawn(_LANE, _LONG, 0.0, clock)
    resumed = _drawn(_LANE, _LONG, 30.0, clock)  # 30 s in: on its way back, a few pixels short of its head
    head = _drawn(_LANE, _LONG, 30.0, LaneClock())
    assert resumed is not None and head is not None and resumed != head and resumed[1] == "ONG "


def test_a_new_presentation_starts_every_line_at_its_head() -> None:
    clock = LaneClock()
    _drawn(_LANE, _LONG, 0.0, clock, presentation="first")
    moved = _drawn(_LANE, _LONG, 5.0, clock, presentation="first")
    fresh = _drawn(_LANE, _LONG, 5.0, clock, presentation="second")  # the same text in the same lane
    assert moved != fresh and fresh is not None and fresh[0] == 0 and _LONG.startswith(fresh[1])


def test_a_frame_the_display_never_took_starts_nothing() -> None:
    clock = LaneClock()
    _drawn(_LANE, _LONG, 0.0, clock, commit=False)  # drawn, but it never reached the panel
    later = _drawn(_LANE, _LONG, 5.0, clock)
    assert later is not None and later[0] == 0 and _LONG.startswith(later[1])  # its line starts here, at its head


def _up_for(clock: LaneClock, seconds: float, *, presentation: str = "card", commit: bool = True) -> float:
    """How long a frame of ``presentation`` at ``seconds`` says its presentation has been up."""
    frame = clock.frame(presentation, MonotonicTime(seconds))
    up = frame.up_for()
    if commit:
        frame.commit()
    return up


def test_a_presentation_has_been_up_since_its_first_frame_was_taken() -> None:
    clock = LaneClock()
    assert _up_for(clock, 100.0, commit=False) == 0.0  # drawn, but the display never took it
    assert _up_for(clock, 101.5) == 0.0  # this one it took: the presentation went up here
    assert _up_for(clock, 107.0) == 5.5
    assert _up_for(clock, 120.25, commit=False) == 18.75


def test_a_new_presentation_goes_up_with_its_own_first_frame() -> None:
    clock = LaneClock()
    _up_for(clock, 10.0, presentation="first")
    assert _up_for(clock, 30.0, presentation="first") == 20.0
    assert _up_for(clock, 31.0, presentation="second") == 0.0
    assert _up_for(clock, 37.0, presentation="second") == 6.0
    assert _up_for(clock, 38.0, presentation="first") == 0.0  # back again: a new time on the board


def test_speeds_raise_the_top_speed_at_one_acceleration_and_keep_the_holds() -> None:
    styles = {speed: DEFAULT_MARQUEE.at(speed) for speed in ScrollSpeed}
    assert [styles[speed].cruise_px_per_s for speed in ScrollSpeed] == [10.0, 12.5, 15.0]
    assert [styles[speed].ramp_s for speed in ScrollSpeed] == [2.0, 2.5, 3.0]
    assert {style.acceleration for style in styles.values()} == {5.0}
    assert {style.hold_s for style in styles.values()} == {2.0}
    assert Marquee() == Marquee(style=DEFAULT_MARQUEE, clock=None)


def test_a_short_trip_turns_at_its_midpoint_without_overshooting() -> None:
    plan = MarqueePlan(distance=5)  # too short for the default ramp to reach cruising speed
    assert plan._along(plan.ramp_time_s) == pytest.approx(2.5)  # exactly halfway, not a full ramp's 10px
    assert plan.offset_at(DEFAULT_MARQUEE.hold_s + plan.travel_s / 2) in (2, 3)
    back = 2 * DEFAULT_MARQUEE.hold_s + plan.travel_s + plan.travel_s / 2
    assert plan.offset_at(back) in (2, 3)  # and the return trip doesn't swing past the head


@pytest.mark.parametrize("speed", list(ScrollSpeed))
@pytest.mark.parametrize("distance", [1, 2, 5, 9, 10, 11, 19, 20, 21, 40, 75])
def test_every_trip_moves_one_way_at_a_time_and_stays_in_bounds(speed: ScrollSpeed, distance: int) -> None:
    plan = MarqueePlan(distance=distance, style=DEFAULT_MARQUEE.at(speed))
    hold, travel = plan.style.hold_s, plan.travel_s
    steps = 200
    outward = [hold + travel * i / steps for i in range(steps + 1)]
    inward = [2 * hold + travel + travel * i / steps for i in range(steps + 1)]
    out = [plan.offset_at(t) for t in outward]
    back = [plan.offset_at(t) for t in inward]
    assert out == sorted(out) and back == sorted(back, reverse=True)
    assert min(out + back) >= 0 and max(out + back) <= distance
    assert out[-1] == distance and back[-1] in (0, distance)  # the tail at the end of the way out
    tick = travel / steps  # the largest step a frame of this length can take, at top speed
    top = plan.style.acceleration * plan.ramp_time_s
    limit = math.ceil(top * tick) + 1  # rounding can add a pixel
    assert max(abs(a - b) for a, b in zip(out, out[1:])) <= limit
    assert max(abs(a - b) for a, b in zip(back, back[1:])) <= limit
