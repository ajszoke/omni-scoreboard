"""Tests for ``omni.core`` value objects: ids, durations, colors."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from omni.core.colors import RGBColor, _ciede2000
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


# Sharma, Wu and Dalal (2005), "The CIEDE2000 Color-Difference Formula: Implementation Notes,
# Supplementary Test Data, and Mathematical Observations", Table 1: all 34 pairs, as
# (L*1, a*1, b*1), (L*2, a*2, b*2), and the published difference to four decimals. Pairs 7 and 8
# hold an achromatic color; pairs 9-16 straddle the hue discontinuities, where the mean hue and
# the hue difference must go the short way round the circle.
_SHARMA_PAIRS = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0010, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0011, -2.4900), 4.7461),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
    ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
    ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
    ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


@pytest.mark.parametrize("lab1, lab2, expected", _SHARMA_PAIRS, ids=[f"pair{n}" for n in range(1, 35)])
def test_ciede2000_matches_the_published_test_data(
    lab1: tuple[float, float, float], lab2: tuple[float, float, float], expected: float
) -> None:
    assert _ciede2000(lab1, lab2) == pytest.approx(expected, abs=1e-4)
    assert _ciede2000(lab2, lab1) == pytest.approx(expected, abs=1e-4)  # the formula is symmetric


def test_delta_e_2000_identity_and_symmetry() -> None:
    navy = RGBColor(12, 35, 64)
    assert navy.delta_e_2000(navy) == 0.0
    silver = RGBColor(196, 206, 212)
    assert navy.delta_e_2000(silver) == pytest.approx(silver.delta_e_2000(navy))


def test_delta_e_2000_reads_two_saturated_reds_as_close_where_cie76_does_not() -> None:
    # The Cardinals' deep red and the Chiefs' bright red, 14 degrees of hue apart: CIE76 counts their
    # lightness and chroma gap at full weight and calls them distinct; CIEDE2000 calls them close.
    ari, kc = RGBColor(151, 35, 63), RGBColor(227, 24, 55)
    assert ari.delta_e(kc) == pytest.approx(37.5, abs=0.1)
    assert ari.delta_e_2000(kc) == pytest.approx(17.0, abs=0.1)


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
