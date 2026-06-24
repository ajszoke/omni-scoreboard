"""Tests for LogoImage (the blittable tile) and LogoStore (the caching loader)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.core.colors import RGBColor
from omni.domain.base import LogoAsset
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.image import LogoImage, LogoStore

REG = MlbTeamRegistry.from_color_file()
COL = REG.resolve(115)  # Rockies; tile background is (51, 0, 111)


def test_from_png_loads_a_20x20_rgb_grid() -> None:
    image = LogoImage.from_png("assets/logos/mlb/lad.png", key="lad")
    assert (image.width, image.height) == (20, 20)
    assert len(image.pixels) == 400
    assert image.pixel(0, 0) == RGBColor(0, 90, 156)  # the flat corner is the team background (Dodger blue)


def test_pixel_count_must_match_dimensions() -> None:
    with pytest.raises(ValueError):
        LogoImage(key="x", width=2, height=2, pixels=(RGBColor(0, 0, 0),))


def test_store_resolves_a_real_team_tile() -> None:
    image = LogoStore().resolve(COL.logo)
    assert image is not None
    assert image.key == "col" and (image.width, image.height) == (20, 20)


def test_store_caches_by_key() -> None:
    store = LogoStore()
    first = store.resolve(COL.logo)
    second = store.resolve(COL.logo)
    assert first is second  # second resolve returns the cached instance, no re-read


def test_store_returns_none_for_a_missing_tile(tmp_path: Path) -> None:
    store = LogoStore(root=tmp_path)  # an empty root: no tiles on disk
    missing = LogoAsset(key="zzz", path="assets/logos/mlb/zzz.png")
    assert store.resolve(missing) is None
    assert store.resolve(missing) is None  # the None is cached too (no repeated stat)
