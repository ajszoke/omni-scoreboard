"""Tests for RenderContext: the immutable ambient inputs handed to a renderer."""

from __future__ import annotations

import dataclasses

import pytest

from omni.core.enum import PanelProfile
from omni.renderers.context import RenderContext


def test_carries_the_panel_profile() -> None:
    ctx = RenderContext(profile=PanelProfile.QUAD_128X64)
    assert ctx.profile is PanelProfile.QUAD_128X64


def test_is_immutable() -> None:
    ctx = RenderContext(profile=PanelProfile.SINGLE_64X32)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.profile = PanelProfile.QUAD_128X64  # type: ignore[misc]


def test_value_equality_by_profile() -> None:
    # Same profile -> equal context; the seam is a plain value object.
    assert RenderContext(profile=PanelProfile.STACK_64X64) == RenderContext(profile=PanelProfile.STACK_64X64)
    assert RenderContext(profile=PanelProfile.STACK_64X64) != RenderContext(profile=PanelProfile.QUAD_128X64)
