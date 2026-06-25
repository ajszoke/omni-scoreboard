"""Tests for ``omni.core`` value objects: ids, durations, colors."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from omni.core.colors import RGBColor
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds


def test_source_ref_defaults_and_equality() -> None:
    a = SourceRef("mlb_statsapi")
    assert a.raw_url is None
    assert a == SourceRef("mlb_statsapi")
    assert a != SourceRef("espn")


def test_league_scoped_id_str_and_hashable() -> None:
    sid = LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "660271")
    assert str(sid) == "mlb:mlb_statsapi:660271"
    # frozen + slots => hashable, usable as dict/set keys.
    assert sid in {sid}
    twin = LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "660271")
    assert {sid: 1}[twin] == 1


def test_value_objects_are_frozen_and_slotted() -> None:
    color = RGBColor(10, 20, 30)
    with pytest.raises(FrozenInstanceError):
        setattr(color, "r", 0)
    assert not hasattr(color, "__dict__")  # slots => no per-instance __dict__


def test_duration_seconds_validates_and_converts() -> None:
    assert DurationSeconds(0).as_timedelta() == timedelta(0)
    assert DurationSeconds(90).as_timedelta() == timedelta(seconds=90)
    with pytest.raises(ValueError):
        DurationSeconds(-1)


def test_rgb_color_validates_range() -> None:
    RGBColor(0, 0, 0)
    RGBColor(255, 255, 255)
    for bad in ((-1, 0, 0), (0, 256, 0), (0, 0, 999)):
        with pytest.raises(ValueError):
            RGBColor(*bad)


def test_rgb_luminance_and_contrast_extremes() -> None:
    white = RGBColor(255, 255, 255)
    black = RGBColor(0, 0, 0)
    assert white.relative_luminance() == pytest.approx(1.0)
    assert black.relative_luminance() == pytest.approx(0.0)
    # WCAG max contrast ratio is 21:1, and it is symmetric.
    assert white.contrast_ratio(black) == pytest.approx(21.0)
    assert black.contrast_ratio(white) == pytest.approx(21.0)


def test_relative_luminance_covers_both_srgb_branches() -> None:
    assert RGBColor(255, 255, 255).relative_luminance() == pytest.approx(1.0)
    assert RGBColor(0, 0, 0).relative_luminance() == pytest.approx(0.0)
    # power-curve branch (channel > 0.03928): mid-gray ~= 0.216
    assert RGBColor(128, 128, 128).relative_luminance() == pytest.approx(0.2159, abs=0.002)
    # linear branch (channel <= 0.03928): near-black stays a small positive value
    assert 0.0 < RGBColor(2, 2, 2).relative_luminance() < 0.001


def test_delta_e_identity_and_symmetry() -> None:
    navy = RGBColor(12, 35, 64)
    assert navy.delta_e(navy) == 0.0  # a colour is identical to itself
    silver = RGBColor(196, 206, 212)
    assert navy.delta_e(silver) == silver.delta_e(navy)  # CIE76 is symmetric


def test_delta_e_black_to_white_spans_the_lightness_range() -> None:
    # The full L* range is 100; this one call exercises both sRGB-linear branches
    # and both Lab-pivot branches (bright cube-root vs dark linear).
    assert RGBColor(0, 0, 0).delta_e(RGBColor(255, 255, 255)) == pytest.approx(100.0, abs=0.01)


def test_delta_e_flags_clashing_navies_but_not_real_accents() -> None:
    navy = RGBColor(12, 35, 64)  # NYY / DET background
    assert navy.delta_e(RGBColor(0, 43, 92)) < 20  # CLE / MIN navy: a clash
    assert navy.delta_e(RGBColor(19, 39, 79)) < 20  # ATL near-navy: a clash
    # A real accent is unambiguously distinct — the basis for an alt background.
    assert navy.delta_e(RGBColor(250, 70, 22)) > 20  # DET orange
    assert navy.delta_e(RGBColor(196, 206, 212)) > 20  # silver


def test_value_lift_leaves_already_legible_colours_untouched() -> None:
    # Anything that already clears the floor on black is returned unchanged (same object).
    for c in (RGBColor(190, 10, 20), RGBColor(255, 199, 44), RGBColor(255, 255, 255)):
        assert c.value_lifted() is c


def test_value_lift_brightens_a_dim_navy_to_the_floor_keeping_hue() -> None:
    navy = RGBColor(0, 50, 99)  # the Angels' dim navy: 1.64:1 on black, washes out
    assert navy.contrast_ratio(RGBColor(0, 0, 0)) < 3.0
    lifted = navy.value_lifted()
    assert lifted.contrast_ratio(RGBColor(0, 0, 0)) >= 3.0  # now legible
    # Pure value raise: the hue is untouched (no red channel introduced; still b > g > r).
    assert lifted.r == 0 and lifted.b > lifted.g > lifted.r


def test_value_lift_desaturates_only_when_brightness_alone_is_not_enough() -> None:
    blue = RGBColor(0, 0, 255)  # already max value, yet only 2.44:1 on black
    lifted = blue.value_lifted()
    assert lifted.contrast_ratio(RGBColor(0, 0, 0)) >= 3.0
    assert lifted.b == 255 and lifted.r > 0 and lifted.g > 0  # value maxed, then desaturated


def test_value_lift_respects_a_custom_floor() -> None:
    red = RGBColor(190, 10, 20)  # 3.24:1 — legible at the default floor, not at 5:1
    assert red.value_lifted() is red
    lifted = red.value_lifted(min_contrast=5.0)
    assert lifted is not red and lifted.contrast_ratio(RGBColor(0, 0, 0)) >= 5.0


def test_value_lift_is_idempotent() -> None:
    once = RGBColor(19, 39, 79).value_lifted()  # ATL navy
    assert once.value_lifted() is once  # a lifted colour already clears the floor


def test_value_lift_settles_at_white_when_the_floor_is_unreachable() -> None:
    # Nothing clears 25:1 on black (white is only 21:1); best effort returns white.
    assert RGBColor(0, 0, 255).value_lifted(min_contrast=25.0) == RGBColor(255, 255, 255)
