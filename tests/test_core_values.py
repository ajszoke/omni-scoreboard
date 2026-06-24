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
