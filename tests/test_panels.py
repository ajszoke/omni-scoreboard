"""Tests for omni.panels geometry across the three supported profiles."""

from __future__ import annotations

from omni.core.enum import PanelProfile
from omni.panels.geometry import PanelGeometry, geometry_for


def test_every_profile_has_geometry() -> None:
    for profile in PanelProfile:
        geometry = geometry_for(profile)
        assert isinstance(geometry, PanelGeometry)
        assert geometry.profile is profile
        assert geometry.width > 0 and geometry.height > 0


def test_geometry_dimensions_match_profile_names() -> None:
    assert geometry_for(PanelProfile.SINGLE_64X32).size == (64, 32)
    assert geometry_for(PanelProfile.STACK_64X64).size == (64, 64)
    assert geometry_for(PanelProfile.QUAD_128X64).size == (128, 64)
