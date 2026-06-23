"""Tests for RenderContext: the immutable ambient inputs handed to a renderer."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from omni.core.enum import PanelProfile
from omni.renderers.context import RenderContext

NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)


def test_carries_profile_and_render_clock() -> None:
    ctx = RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW)
    assert ctx.profile is PanelProfile.QUAD_128X64
    assert ctx.now == NOW


def test_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        RenderContext(profile=PanelProfile.QUAD_128X64, now=datetime(2026, 6, 17, 23, 30))


def test_is_immutable() -> None:
    ctx = RenderContext(profile=PanelProfile.SINGLE_64X32, now=NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.profile = PanelProfile.QUAD_128X64  # type: ignore[misc]


def test_value_equality() -> None:
    # Same fields -> equal context; the seam is a plain value object.
    assert RenderContext(profile=PanelProfile.STACK_64X64, now=NOW) == RenderContext(
        profile=PanelProfile.STACK_64X64, now=NOW
    )
    assert RenderContext(profile=PanelProfile.STACK_64X64, now=NOW) != RenderContext(
        profile=PanelProfile.QUAD_128X64, now=NOW
    )
